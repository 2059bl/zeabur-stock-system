"""
產業委屈股篩選系統 v1.0
========================
- 每日盤後（16:00）更新股價
- 每月 10 日（09:00）執行完整財務篩選
- Telegram 推播 + Web 儀表板
"""
import os
import asyncio
import logging
import datetime
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from agents.screener import run_screening, save_results
from utils.db import get_pool, fetch_all, execute
from utils.notifier import send
from utils.price import fetch_ohlcv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger    = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="Asia/Taipei")
_TZ8      = datetime.timezone(datetime.timedelta(hours=8))

# ── 產業股票池（含四個新主題池）───────────────────────────────────────────────
# cfg 欄位覆寫 Layer 1 門檻；不設則沿用預設值
_IC_POOLS = [
    {"name": "IC設計",      "description": "IC Design，包含類比、數位、混訊設計廠", "cfg": {}},
    {"name": "IC製造/代工",  "description": "晶圓代工與IDM製造廠",                  "cfg": {}},
    {"name": "IC封裝測試",   "description": "封裝、測試、基板廠",                    "cfg": {}},
    {
        "name": "mSAP/ABF基板",
        "description": "修正型半加成製程PCB與ABF載板，AI伺服器基板需求受益",
        "cfg": {"pe_max": 25},   # 技術溢價合理，PE 放寬至 25
    },
    {
        "name": "CCL銅箔基板",
        "description": "銅箔基板材料供應鏈：玻纖布→銅箔→CCL→特殊化工",
        "cfg": {"debt_max": 60}, # 材料/化工股資本密集，負債比放寬至 60%
    },
    {
        "name": "LTA存儲板塊",
        "description": "記憶體與存儲控制器，AI長約採購(LTA)帶動週期反轉",
        "cfg": {"cum_growth_min": 15, "pe_max": 30},  # 週期谷底回升，門檻放寬
    },
    {
        "name": "HBM伺服器",
        "description": "HBM先進封裝測試＋AI伺服器整機供應鏈",
        "cfg": {"vol_min": 500}, # 部分小型供應商流動性較低
    },
    {
        "name": "被動元件",
        "description": "MLCC/電阻/電感/鐵氧體/二極體，AI備料拉貨受益",
        "cfg": {"pe_max": 25},   # 缺料週期被動元件享有溢價，PE 放寬至 25
    },
    {
        "name": "PCB",
        "description": "印刷電路板全產業鏈：多層板、軟板、HDI至ABF載板",
        "cfg": {},               # 預設門檻
    },
    {
        "name": "連接器與散熱",
        "description": "連接器、線纜、散熱模組、風扇，AI伺服器熱管理需求受益",
        "cfg": {"vol_min": 500},
    },
    {
        "name": "面板與顯示器",
        "description": "TFT-LCD/OLED面板、驅動IC、背光模組，AI終端顯示器需求受益",
        "cfg": {"pe_max": 25, "debt_max": 60},  # 面板廠資本密集+週期復甦溢價
    },
    {
        "name": "LED與光學鏡頭",
        "description": "LED磊晶/封裝、光學鏡頭、背光模組，Mini/Micro LED + AR/VR 受益",
        "cfg": {},  # 預設門檻
    },
    {
        "name": "伺服器與資料中心",
        "description": "AI伺服器ODM、電源供應器、機架，資料中心算力擴充受益",
        "cfg": {"pe_max": 25},  # AI伺服器需求高漲，PE 放寬至 25
    },
    {
        "name": "品牌與代工PC/NB",
        "description": "PC/NB品牌廠與ODM/EMS，AI PC換機潮受益",
        "cfg": {},  # ODM毛利薄，PE通常已低於20
    },
]

# 各池名稱 → cfg 映射（排程時查表）
_POOL_CFG_BY_NAME = {p["name"]: p.get("cfg", {}) for p in _IC_POOLS}

_IC_STOCKS = {
    "IC設計": [
        ("2454", "聯發科"),  ("2379", "瑞昱"),   ("3034", "聯詠"),
        ("6415", "矽力-KY"), ("4966", "譜瑞-KY"), ("3661", "世芯-KY"),
        ("3443", "創意"),    ("6533", "晶心科"),  ("3529", "力旺"),
        ("2388", "威盛"),    ("3665", "奇景光電"), ("5274", "信驊"),
        ("2436", "偉詮電"),  ("4919", "新唐"),    ("6643", "M31"),
        ("6230", "超豐"),    ("3707", "漢磊"),    ("5222", "全訊"),
        ("6756", "恩智浦-TW"), ("3532", "台勝科"),
    ],
    "IC製造/代工": [
        ("2330", "台積電"),  ("2303", "聯電"),    ("5347", "世界先進"),
        ("6770", "力積電"),  ("3105", "穩懋"),    ("2449", "京元電子"),
        ("4523", "建準"),    ("3008", "大立光"),
    ],
    "IC封裝測試": [
        ("3711", "日月光投控"), ("2449", "京元電子"), ("6257", "矽格"),
        ("2441", "超豐"),      ("6239", "力成"),     ("8150", "南茂"),
        ("3264", "欣銓"),      ("2442", "新美齊"),   ("6271", "同欣電"),
        ("5344", "立積"),      ("3010", "華立"),     ("6214", "精材"),
    ],
    # ── 四個新主題池 ─────────────────────────────────────────────────────────
    "mSAP/ABF基板": [
        # ABF/BT 載板核心廠
        ("3037", "欣興"),    ("8046", "南電"),    ("3189", "景碩"),
        # 特殊積層材料/PCB 銅箔基板材料
        ("6274", "台燿"),    ("2383", "台光電"),
        # PCB 化學品/材料供應商
        ("5285", "界霖"),    ("4105", "弘凱"),
        # 多層 PCB 廠（mSAP 製程）
        ("8261", "志超"),    ("6119", "旭碁"),    ("3533", "嘉聯益"),
        ("2328", "廣宇"),
    ],
    "CCL銅箔基板": [
        # 銅箔（電解銅箔）
        ("2038", "海光"),
        # CCL 積層板製造
        ("6438", "聯茂"),    ("2383", "台光電"),  ("6274", "台燿"),
        # 上游原材料：南亞塑膠（環氧樹脂+CCL事業部）
        ("1303", "南亞"),
        # 玻纖布
        ("1325", "恆大"),
        # 環氧樹脂
        ("1312", "國喬"),    ("1304", "台聚"),
        # 石化/高分子基材
        ("1326", "台化"),
    ],
    "LTA存儲板塊": [
        # DRAM 製造
        ("2408", "南亞科"),
        # NAND Flash 控制器
        ("8299", "群聯"),    ("6279", "慧榮"),
        # DRAM 模組/通路
        ("3260", "威剛"),    ("8112", "至上"),
        # SRAM / 特殊記憶體
        ("3006", "晶豪科"),
        # NOR Flash + MCU
        ("4919", "新唐"),
        # 特殊製程晶圓代工（記憶體周邊）
        ("5347", "世界先進"),
        # 主機板（記憶體平台需求端）
        ("3515", "華擎"),
        # 矽晶圓（DRAM 上游）
        ("5483", "中美晶"),
    ],
    "HBM伺服器": [
        # HBM 先進封裝/CoWoS 受益
        ("3711", "日月光投控"), ("6239", "力成"),    ("3264", "欣銓"),
        ("8150", "南茂"),
        # AI 伺服器 ODM/整機
        ("6669", "緯穎"),    ("3231", "緯創"),    ("2356", "英業達"),
        ("4938", "和碩"),
        # 散熱/電源管理
        ("3017", "奇鋐"),    ("6415", "矽力-KY"),
        # 伺服器管理 IC / BMC
        ("5274", "信驊"),
        # IC 通路（AI 伺服器零組件）
        ("3036", "文曄"),
        # 高速連接 / PCIe
        ("4966", "譜瑞-KY"),
        # GaN RF / 功率
        ("3105", "穩懋"),
    ],
    # ── 三個新通用元件池 ──────────────────────────────────────────────────────
    "被動元件": [
        # MLCC / 電阻 — 台灣前兩大被動元件廠
        ("2327", "國巨"),    ("2492", "華新科"),
        # 電感 / 功率磁性元件 / 散熱風扇（台達電被動+電源部門）
        ("2308", "台達電"),
        # 陶瓷電容（Tai-Tech / 百容）
        ("2483", "百容"),
        # 鐵氧體磁芯 / EMI 濾波器
        ("3557", "嘉彰"),
        # 晶片二極體 / TVS（德微科技）
        ("3675", "德微"),
        # 整流二極體 / MOSFET（強茂電子）
        ("2481", "強茂"),
        # 微調電位器 / 可變電容（冠西電子）
        ("2466", "冠西電"),
    ],
    "PCB": [
        # ABF/HDI 載板（高階）
        ("3037", "欣興"),    ("8046", "南電"),    ("3189", "景碩"),
        # 汽車 + 工業多層板
        ("3044", "健鼎"),
        # 一般多層 PCB
        ("2313", "華通"),    ("8261", "志超"),    ("6119", "旭碁"),
        ("3533", "嘉聯益"),  ("2328", "廣宇"),
        # 特殊積層材料 / 軟性銅箔基板
        ("2383", "台光電"),
        # PCB 化學品供應商
        ("5285", "界霖"),    ("4105", "弘凱"),
    ],
    "連接器與散熱": [
        # 汽車線束 / USB 連接器
        ("6088", "正崴"),
        # 汽車連接器（慶良電子）
        ("6204", "慶良"),
        # 電源線 / 連接器（良維工業）
        ("6290", "良維"),
        # 散熱模組 / 熱管 / 均熱板（奇鋐科技）
        ("3017", "奇鋐"),
        # 筆電 / 伺服器散熱模組（雙鴻科技）
        ("3324", "雙鴻"),
        # 伺服器機架 / 熱管理（川湖科技）
        ("2059", "川湖"),
        # 伺服器散熱風扇（建準電機）
        ("2421", "建準"),
        # 電源供應器 + 散熱風扇（台達電）
        ("2308", "台達電"),
        # 電線電纜 / 電氣互連（美亞銅線）
        ("2020", "美亞"),
    ],
    # ── 四個新終端市場池 ────────────────────────────────────────────────────────
    "面板與顯示器": [
        # 大尺寸 TFT-LCD 面板
        ("2409", "友達"),    ("3481", "群創"),
        # 中小尺寸 LCD（彩晶）
        ("6116", "彩晶"),
        # 顯示驅動 IC
        ("3034", "聯詠"),    ("3665", "奇景光電"),
        # 高速顯示介面 IC（DisplayPort/HDMI retimer）
        ("4966", "譜瑞-KY"),
        # LED 背光模組
        ("6120", "達運"),
        # 投影機 / 背光光學模組（中光電）
        ("5371", "中光電"),
        # 軟性電路板（顯示器 FPC）
        ("6269", "台郡"),
    ],
    "LED與光學鏡頭": [
        # LED 磊晶（晶電 Epistar）
        ("2448", "晶電"),
        # LED 元件（光磊 Opto Tech）
        ("2340", "光磊"),
        # LED 封裝（隆達電子）
        ("3698", "隆達"),
        # LED 照明應用（佰鴻工業）
        ("3031", "佰鴻"),
        # 背光模組（達運精密）
        ("6120", "達運"),
        # 智慧型手機光學鏡頭龍頭（大立光）
        ("3008", "大立光"),
        # 投影機 / 光學模組（中光電）
        ("5371", "中光電"),
        # 投影機 / 醫療顯示 ODM（佳世達）
        ("2352", "佳世達"),
    ],
    "伺服器與資料中心": [
        # NB/Server ODM 龍頭（廣達）
        ("2382", "廣達"),
        # 超大規模 AI 伺服器（緯穎）
        ("6669", "緯穎"),
        # Server + NB ODM（緯創）
        ("3231", "緯創"),
        # Server + NB ODM（英業達）
        ("2356", "英業達"),
        # ODM / EMS 龍頭（鴻海）
        ("2317", "鴻海"),
        # 伺服器 PSU + 散熱風扇（台達電）
        ("2308", "台達電"),
        # 光電元件 + 伺服器 PSU（光寶科技）
        ("2301", "光寶科技"),
        # 伺服器機架滑軌 + 機構件（川湖科技）
        ("2059", "川湖"),
        # 伺服器 BMC 管理 IC（信驊）
        ("5274", "信驊"),
        # 伺服器散熱（奇鋐）
        ("3017", "奇鋐"),
    ],
    "品牌與代工PC/NB": [
        # 品牌廠
        ("2357", "華碩"),    ("2353", "宏碁"),
        # NB ODM / 代工
        ("2382", "廣達"),    ("3231", "緯創"),    ("4938", "和碩"),
        # EMS 龍頭
        ("2317", "鴻海"),
        # 主機板品牌
        ("2376", "技嘉"),    ("3515", "華擎"),    ("2399", "映泰"),
        # 電競 NB 品牌（藍天）
        ("2362", "藍天"),
        # NB + 工業電腦 ODM（神達）
        ("3706", "神達"),
        # 投影機 / NB ODM（佳世達）
        ("2352", "佳世達"),
    ],
}

# ── DB Schema ─────────────────────────────────────────────────────────────────
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS screener_pools (
    pool_id     SERIAL PRIMARY KEY,
    pool_name   VARCHAR(50) UNIQUE NOT NULL,
    description TEXT,
    is_active   BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS screener_stocks (
    stock_code  VARCHAR(10) PRIMARY KEY,
    stock_name  VARCHAR(50) NOT NULL,
    market      VARCHAR(10) DEFAULT 'TWSE',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS screener_pool_stocks (
    pool_id     INT  REFERENCES screener_pools(pool_id) ON DELETE CASCADE,
    stock_code  VARCHAR(10) REFERENCES screener_stocks(stock_code),
    is_active   BOOLEAN DEFAULT TRUE,
    added_at    TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (pool_id, stock_code)
);

CREATE TABLE IF NOT EXISTS screener_results (
    id               BIGSERIAL PRIMARY KEY,
    pool_id          INT  REFERENCES screener_pools(pool_id),
    screen_date      DATE NOT NULL,
    rank             INT,
    stock_code       VARCHAR(10),
    stock_name       VARCHAR(50),
    score            NUMERIC(4,1),
    pe_ratio         NUMERIC(7,1),
    cum_rev_growth   NUMERIC(7,2),
    q1_eps           NUMERIC(8,2),
    h1_profit_growth NUMERIC(7,2),
    roe              NUMERIC(7,2),
    debt_ratio       NUMERIC(7,2),
    capital_bn       NUMERIC(8,2),
    close_price      NUMERIC(10,2),
    avg_vol_3d       NUMERIC(10,0),
    m1_pct           NUMERIC(7,2),
    q1_pct           NUMERIC(7,2),
    dist_high_pct    NUMERIC(7,2),
    score_reasons    TEXT,
    updated_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (pool_id, screen_date, stock_code)
);

CREATE TABLE IF NOT EXISTS screener_logs (
    id          BIGSERIAL PRIMARY KEY,
    pool_id     INT,
    run_date    DATE NOT NULL,
    job_type    VARCHAR(20) NOT NULL,  -- 'PRICE_UPDATE' / 'FULL_SCREEN'
    status      VARCHAR(20) NOT NULL DEFAULT 'RUNNING',
    candidates  INT,
    error_msg   TEXT,
    started_at  TIMESTAMPTZ DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);
"""


async def _seed_pools_and_stocks():
    """首次啟動時植入產業池與股票資料。"""
    for pool in _IC_POOLS:
        await execute("""
            INSERT INTO screener_pools (pool_name, description)
            VALUES ($1, $2)
            ON CONFLICT (pool_name) DO NOTHING
        """, pool["name"], pool["description"])

    for pool_name, stocks in _IC_STOCKS.items():
        pool_row = await fetch_all(
            "SELECT pool_id FROM screener_pools WHERE pool_name = $1", pool_name
        )
        if not pool_row:
            continue
        pool_id = pool_row[0]["pool_id"]
        for code, name in stocks:
            await execute("""
                INSERT INTO screener_stocks (stock_code, stock_name)
                VALUES ($1, $2)
                ON CONFLICT (stock_code) DO NOTHING
            """, code, name)
            await execute("""
                INSERT INTO screener_pool_stocks (pool_id, stock_code)
                VALUES ($1, $2)
                ON CONFLICT (pool_id, stock_code) DO NOTHING
            """, pool_id, code)

    logger.info("產業池與股票資料植入完成")


# ── 排程工作 ──────────────────────────────────────────────────────────────────

async def _daily_price_update():
    """每日 16:00：更新所有追蹤股的股價（存入 screener_logs）。"""
    _tz   = datetime.timezone(datetime.timedelta(hours=8))
    today = datetime.datetime.now(_tz).date()
    log   = await fetch_all(
        "INSERT INTO screener_logs (pool_id, run_date, job_type) VALUES (NULL,$1,'PRICE_UPDATE') RETURNING id",
        today
    )
    log_id = log[0]["id"] if log else None
    try:
        stocks = await fetch_all("SELECT DISTINCT stock_code FROM screener_pool_stocks WHERE is_active=TRUE")
        updated = 0
        for s in stocks:
            rows = await fetch_ohlcv(s["stock_code"])
            if rows:
                updated += 1
        logger.info(f"[Price] {today} 股價更新：{updated}/{len(stocks)} 檔成功")
        if log_id:
            await execute("UPDATE screener_logs SET status='SUCCESS', finished_at=NOW() WHERE id=$1", log_id)
    except Exception as e:
        logger.exception(f"股價更新失敗: {e}")
        if log_id:
            await execute("UPDATE screener_logs SET status='FAILED', error_msg=$2, finished_at=NOW() WHERE id=$1", log_id, str(e)[:500])


async def _monthly_full_screen():
    """每月 10 日：對所有產業池執行完整財務篩選 + 推播結果。"""
    _tz   = datetime.timezone(datetime.timedelta(hours=8))
    today = datetime.datetime.now(_tz).date()
    pools = await fetch_all("SELECT pool_id, pool_name FROM screener_pools WHERE is_active=TRUE")

    all_msgs = []
    for pool in pools:
        pid   = pool["pool_id"]
        pname = pool["pool_name"]
        log   = await fetch_all(
            "INSERT INTO screener_logs (pool_id, run_date, job_type) VALUES ($1,$2,'FULL_SCREEN') RETURNING id",
            pid, today
        )
        log_id = log[0]["id"] if log else None
        try:
            pool_cfg   = _POOL_CFG_BY_NAME.get(pname, {})
            candidates = await run_screening(pid, today, pool_cfg=pool_cfg)
            await save_results(candidates)
            if log_id:
                await execute(
                    "UPDATE screener_logs SET status='SUCCESS', candidates=$2, finished_at=NOW() WHERE id=$1",
                    log_id, len(candidates)
                )
            if candidates:
                lines = [f"🔍 *{pname} 委屈股*（{today}，共{len(candidates)}檔）\n"]
                for c in candidates[:8]:
                    lines.append(
                        f"#{c.get('rank','-')} `{c['stock_code']}` {c['stock_name']}  "
                        f"得分{c['score']}  PE{c.get('pe_ratio','—')}  "
                        f"月漲{c.get('m1_pct','—')}%"
                    )
                all_msgs.append("\n".join(lines))
        except Exception as e:
            logger.exception(f"Pool {pid} 篩選失敗: {e}")
            if log_id:
                await execute(
                    "UPDATE screener_logs SET status='FAILED', error_msg=$2, finished_at=NOW() WHERE id=$1",
                    log_id, str(e)[:500]
                )

    # 整合推播
    if all_msgs:
        await send("\n\n".join(all_msgs))
    else:
        await send(f"📊 {today} 月度委屈股篩選完成，本月無符合條件標的")


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)
    await _seed_pools_and_stocks()
    logger.info("Schema 初始化完成")

    # 每日 16:00 股價更新
    scheduler.add_job(
        _daily_price_update,
        CronTrigger(hour=16, minute=0, timezone="Asia/Taipei"),
        id="daily_price", replace_existing=True,
    )
    # 每月 10 日 09:00 完整篩選
    scheduler.add_job(
        _monthly_full_screen,
        CronTrigger(day=10, hour=9, minute=0, timezone="Asia/Taipei"),
        id="monthly_screen", replace_existing=True,
    )
    scheduler.start()
    logger.info("排程啟動：每日 16:00 更新股價 / 每月10日 09:00 完整篩選")
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="產業委屈股篩選系統", version="1.5.0", lifespan=lifespan)


# ── API Routes ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    _tz = datetime.timezone(datetime.timedelta(hours=8))
    pools = await fetch_all("SELECT COUNT(*) AS n FROM screener_pools WHERE is_active=TRUE")
    stocks = await fetch_all("SELECT COUNT(*) AS n FROM screener_pool_stocks WHERE is_active=TRUE")
    return {
        "status":  "ok",
        "version": "1.5.0",
        "time":    datetime.datetime.now(_tz).isoformat(),
        "pools":   pools[0]["n"] if pools else 0,
        "stocks":  stocks[0]["n"] if stocks else 0,
    }


@app.get("/pools")
async def list_pools():
    return await fetch_all("SELECT * FROM screener_pools ORDER BY pool_id")


@app.get("/pools/{pool_id}/stocks")
async def list_pool_stocks(pool_id: int):
    return await fetch_all("""
        SELECT ps.stock_code, s.stock_name, ps.is_active, ps.added_at
        FROM screener_pool_stocks ps
        JOIN screener_stocks s ON s.stock_code = ps.stock_code
        WHERE ps.pool_id = $1
        ORDER BY ps.stock_code
    """, pool_id)


@app.post("/pools/{pool_id}/stocks")
async def add_stock_to_pool(pool_id: int, stock_code: str, stock_name: str):
    await execute("""
        INSERT INTO screener_stocks (stock_code, stock_name)
        VALUES ($1, $2) ON CONFLICT (stock_code) DO NOTHING
    """, stock_code, stock_name)
    await execute("""
        INSERT INTO screener_pool_stocks (pool_id, stock_code)
        VALUES ($1, $2) ON CONFLICT DO NOTHING
    """, pool_id, stock_code)
    return {"status": "ok", "pool_id": pool_id, "stock_code": stock_code}


@app.delete("/pools/{pool_id}/stocks/{stock_code}")
async def remove_stock_from_pool(pool_id: int, stock_code: str):
    await execute(
        "UPDATE screener_pool_stocks SET is_active=FALSE WHERE pool_id=$1 AND stock_code=$2",
        pool_id, stock_code
    )
    return {"status": "ok"}


@app.post("/run/screen")
async def manual_screen(
    pool_id: Optional[int] = Query(None, description="指定產業池，空則跑全部"),
    trade_date: Optional[str] = Query(None, description="YYYY-MM-DD，預設今日"),
):
    """手動觸發篩選（背景執行）。"""
    import asyncio
    _tz = datetime.timezone(datetime.timedelta(hours=8))
    td  = datetime.date.fromisoformat(trade_date) if trade_date else datetime.datetime.now(_tz).date()
    asyncio.create_task(_monthly_full_screen())
    return {"status": "篩選已觸發", "date": str(td), "pools": len(_IC_POOLS)}


@app.get("/results")
async def get_results(
    pool_id: Optional[int] = Query(None),
    screen_date: Optional[str] = Query(None),
    limit: int = Query(20, le=100),
):
    """取得篩選結果。"""
    _tz = datetime.timezone(datetime.timedelta(hours=8))
    td  = (datetime.date.fromisoformat(screen_date)
           if screen_date else None)
    if td:
        sql = """
            SELECT r.*, p.pool_name
            FROM screener_results r
            JOIN screener_pools p ON p.pool_id = r.pool_id
            WHERE r.screen_date = $1 {pool_filter}
            ORDER BY r.pool_id, r.rank
            LIMIT $2
        """
        args: list = [td, limit]
    else:
        sql = """
            SELECT r.*, p.pool_name
            FROM screener_results r
            JOIN screener_pools p ON p.pool_id = r.pool_id
            WHERE r.screen_date = (SELECT MAX(screen_date) FROM screener_results {pool_sub})
            {pool_filter}
            ORDER BY r.pool_id, r.rank
            LIMIT $1
        """
        args = [limit]

    if pool_id:
        sql = sql.replace("{pool_filter}", f"AND r.pool_id = {pool_id}") \
                 .replace("{pool_sub}",    f"WHERE pool_id = {pool_id}")
    else:
        sql = sql.replace("{pool_filter}", "").replace("{pool_sub}", "")

    return await fetch_all(sql, *args)


@app.get("/logs")
async def get_logs(limit: int = Query(10, le=50)):
    return await fetch_all("""
        SELECT l.*, p.pool_name
        FROM screener_logs l
        LEFT JOIN screener_pools p ON p.pool_id = l.pool_id
        ORDER BY started_at DESC LIMIT $1
    """, limit)


# ── Web 儀表板 ────────────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(pool: Optional[int] = Query(None, description="直接開啟指定 pool_id")):
    _tz    = datetime.timezone(datetime.timedelta(hours=8))
    now_str = datetime.datetime.now(_tz).strftime("%Y-%m-%d %H:%M")

    pools, results, logs, stock_cnt = await asyncio.gather(
        fetch_all("SELECT * FROM screener_pools WHERE is_active=TRUE ORDER BY pool_id"),
        fetch_all("""
            SELECT r.*, p.pool_name
            FROM screener_results r
            JOIN screener_pools p ON p.pool_id = r.pool_id
            WHERE r.screen_date = (SELECT MAX(screen_date) FROM screener_results)
            ORDER BY r.pool_id, r.rank
            LIMIT 60
        """),
        fetch_all("SELECT l.*, p.pool_name FROM screener_logs l LEFT JOIN screener_pools p ON p.pool_id=l.pool_id ORDER BY started_at DESC LIMIT 20"),
        fetch_all("SELECT COUNT(*) AS n FROM screener_pool_stocks WHERE is_active=TRUE"),
    )

    screen_date = str(results[0]["screen_date"]) if results else "尚未執行篩選"
    total_stocks = stock_cnt[0]["n"] if stock_cnt else 0

    def score_bar(s):
        w = min(int(float(s or 0) / 13 * 100), 100)
        color = "#22c55e" if w >= 60 else "#f59e0b" if w >= 40 else "#60a5fa"
        return (f'<div style="display:flex;align-items:center;gap:6px">'
                f'<div style="width:60px;background:#1e293b;border-radius:3px;height:7px">'
                f'<div style="width:{w}%;background:{color};height:7px;border-radius:3px"></div></div>'
                f'<b style="font-size:12px">{s}</b></div>')

    def pct_cell(v, good_above=0):
        if v is None: return '<span style="color:#475569">—</span>'
        v = float(v)
        c = "#22c55e" if v > good_above else "#ef4444" if v < 0 else "#94a3b8"
        return f'<span style="color:{c}">{v:+.1f}%</span>'

    # 依 pool 分組
    pool_tabs  = ""
    pool_html  = ""
    for pool in pools:
        pid   = pool["pool_id"]
        pname = pool["pool_name"]
        prows = [r for r in results if r["pool_id"] == pid]

        pool_tabs += (
            f'<span style="display:inline-flex;align-items:center;gap:2px">'
            f'<button class="pool-tab" id="tab-{pid}" onclick="switchPool({pid})">{pname}（{len(prows)}）</button>'
            f'<a href="/dashboard?pool={pid}" title="複製池連結" style="color:#334155;font-size:11px;padding:4px 5px;border-radius:4px;text-decoration:none;line-height:1" '
            f'onmouseover="this.style.color=\'#38bdf8\'" onmouseout="this.style.color=\'#334155\'">🔗</a>'
            f'</span>'
        )

        rows_html = ""
        for r in prows:
            rows_html += f"""<tr>
              <td style="color:#64748b">{r['rank']}</td>
              <td><span style="color:#38bdf8;font-weight:700">{r['stock_code']}</span></td>
              <td>{r['stock_name']}</td>
              <td>{score_bar(r['score'])}</td>
              <td style="color:#f59e0b">{r.get('pe_ratio') or '—'}</td>
              <td>{pct_cell(r.get('cum_rev_growth'), 0)}</td>
              <td style="color:#22c55e">{r.get('q1_eps') or '—'}</td>
              <td>{pct_cell(r.get('h1_profit_growth'), 0)}</td>
              <td>{r.get('roe') or '—'}%</td>
              <td>{pct_cell(r.get('m1_pct'), 6)}</td>
              <td>{pct_cell(r.get('q1_pct'))}</td>
              <td style="color:#94a3b8">{r.get('avg_vol_3d') or '—'}</td>
              <td style="font-size:11px;color:#64748b">{(r.get('score_reasons') or '')[:50]}</td>
            </tr>"""

        pool_html += f"""
        <div id="pool-{pid}" class="pool-section" style="display:none">
          <div style="overflow-x:auto">
          <table>
            <thead><tr>
              <th>#</th><th>代碼</th><th>名稱</th><th>委屈分</th>
              <th>PE</th><th>累計營收</th><th>Q1 EPS</th><th>上半年獲利</th>
              <th>ROE</th><th>月漲幅</th><th>季漲幅</th><th>3日均量</th><th>加分原因</th>
            </tr></thead>
            <tbody>{rows_html or '<tr><td colspan="13" style="text-align:center;padding:30px;color:#475569">本月無入選標的</td></tr>'}</tbody>
          </table>
          </div>
        </div>"""

    log_html = ""
    for l in logs:
        sc = {"SUCCESS":"#22c55e","FAILED":"#ef4444","RUNNING":"#f59e0b"}.get(l["status"],"#94a3b8")
        bg = {"SUCCESS":"#166534","FAILED":"#7f1d1d","RUNNING":"#78350f"}.get(l["status"],"#1e293b")
        log_html += f"""<tr>
          <td style="color:#94a3b8">{l.get('pool_name') or '全部'}</td>
          <td style="color:#94a3b8">{l['run_date']}</td>
          <td><span style="background:{bg};color:{sc};padding:2px 8px;border-radius:999px;font-size:11px">{l['status']}</span></td>
          <td style="color:#64748b;font-size:11px">{l['job_type']}</td>
          <td>{l.get('candidates') or '—'}</td>
          <td style="font-size:11px;color:#64748b">{(l.get('error_msg') or '')[:50]}</td>
        </tr>"""

    first_pid = pool if pool else (pools[0]["pool_id"] if pools else 1)

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>產業委屈股篩選系統</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0a0f1e;color:#e2e8f0;font-family:-apple-system,'PingFang TC','Microsoft JhengHei',sans-serif}}
  .navbar{{background:#0f172a;border-bottom:1px solid #1e293b;padding:10px 20px;
    display:flex;align-items:center;gap:6px;flex-wrap:wrap;position:sticky;top:0;z-index:101}}
  .navbar span{{font-size:13px;color:#38bdf8;font-weight:700;margin-right:8px}}
  .navbar a{{font-size:12px;color:#64748b;text-decoration:none;padding:4px 10px;
    border-radius:4px;border:1px solid #1e293b}}
  .navbar a:hover,.navbar a.active{{color:#e2e8f0;background:#1e293b}}
  .topbar{{background:#0f172a;border-bottom:1px solid #1e293b;padding:12px 24px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:41px;z-index:100}}
  .topbar h1{{font-size:17px;color:#38bdf8;font-weight:700}}
  .topbar .meta{{font-size:12px;color:#475569;display:flex;gap:16px;align-items:center}}
  .dot{{width:8px;height:8px;border-radius:50%;background:#22c55e;animation:pulse 2s infinite}}
  @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.4}}}}
  .main{{padding:20px 24px;max-width:1500px;margin:0 auto}}
  .stat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}}
  .stat{{background:#0f172a;border:1px solid #1e293b;border-radius:10px;padding:14px 16px}}
  .stat .lbl{{font-size:11px;color:#64748b;margin-bottom:4px}}
  .stat .val{{font-size:24px;font-weight:700;color:#e2e8f0}}
  .stat .sub{{font-size:11px;color:#475569;margin-top:2px}}
  .tabs{{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:16px;border-bottom:1px solid #1e293b;padding-bottom:8px}}
  .tab{{padding:7px 18px;border-radius:6px 6px 0 0;font-size:13px;cursor:pointer;color:#64748b;border:none;background:none;font-family:inherit}}
  .tab.active{{background:#1e293b;color:#e2e8f0;font-weight:600}}
  .page{{display:none}}.page.active{{display:block}}
  .card{{background:#0f172a;border:1px solid #1e293b;border-radius:10px;padding:16px 20px;margin-bottom:20px}}
  .sec-title{{font-size:12px;color:#64748b;text-transform:uppercase;letter-spacing:.8px;margin-bottom:12px}}
  table{{width:100%;border-collapse:collapse;font-size:13px}}
  th{{background:#080d1a;color:#475569;padding:9px 8px;text-align:left;font-size:11px;letter-spacing:.4px;border-bottom:1px solid #1e293b;white-space:nowrap}}
  td{{padding:9px 8px;border-bottom:1px solid #0a0f1e;white-space:nowrap}}
  tr:hover td{{background:#111827}}
  .pool-tabs{{display:flex;gap:4px;margin-bottom:12px;flex-wrap:wrap}}
  .pool-tab{{padding:6px 14px;border-radius:6px;font-size:12px;cursor:pointer;color:#64748b;border:1px solid #1e293b;background:none;font-family:inherit}}
  .pool-tab.active{{background:#1e293b;color:#e2e8f0;border-color:#334155}}
</style>
</head>
<body>
<div class="navbar">
  <span>🐻 台股監控</span>
  <a href="https://twstock-agent-1781283629.zeabur.app/dashboard">📊 量化系統</a>
  <a href="https://momentum-screener.zeabur.app/dashboard">⚡ 動量篩選</a>
  <a href="https://ic-screener.zeabur.app/dashboard" class="active">🔬 委屈股</a>
  <a href="https://bear-signal-service.zeabur.app/dashboard">🐻 空頭信號</a>
  <a href="https://bear-signal-service.zeabur.app/stop-loss">🛑 停損預警</a>
</div>
<div class="topbar">
  <h1>🔬 產業委屈股篩選系統</h1>
  <div class="meta">
    <div class="dot"></div>
    <span>排程 16:00　財報 每月10日</span>
    <span>更新：{now_str}</span>
    <span id="cd" style="color:#38bdf8"></span>
    <button onclick="triggerScreen()" style="background:#1e293b;color:#38bdf8;border:1px solid #334155;padding:5px 12px;border-radius:6px;font-size:12px;cursor:pointer">▶ 立即篩選</button>
  </div>
</div>
<div class="main">

<div class="stat-grid">
  <div class="stat"><div class="lbl">追蹤產業池</div><div class="val" style="color:#38bdf8">{len(pools)}</div><div class="sub">14個細分池</div></div>
  <div class="stat"><div class="lbl">追蹤股票</div><div class="val">{total_stocks}</div><div class="sub">跨產業合計</div></div>
  <div class="stat"><div class="lbl">本月入選</div><div class="val" style="color:#22c55e">{len(results)}</div><div class="sub">委屈股候選</div></div>
  <div class="stat"><div class="lbl">最新篩選</div><div class="val" style="font-size:14px;padding-top:4px">{screen_date}</div><div class="sub">每日 16:00 更新</div></div>
</div>

<div class="tabs">
  <button class="tab active" onclick="switchPage('results')">📊 篩選結果</button>
  <button class="tab" onclick="switchPage('logs')">📋 執行紀錄</button>
</div>

<div id="results" class="page active">
  <div class="card">
    <div class="sec-title">產業池選擇</div>
    <div class="pool-tabs">
      {pool_tabs}
    </div>
    {pool_html}
  </div>
</div>

<div id="logs" class="page">
  <div class="card">
    <div class="sec-title">排程執行紀錄</div>
    <table>
      <thead><tr><th>產業池</th><th>日期</th><th>狀態</th><th>工作類型</th><th>入選數</th><th>錯誤</th></tr></thead>
      <tbody>{log_html or '<tr><td colspan="6" style="text-align:center;padding:20px;color:#475569">尚無紀錄</td></tr>'}</tbody>
    </table>
  </div>
</div>

</div>
<script>
var curPool = {first_pid};
function switchPage(id){{
  document.querySelectorAll('.page').forEach(e=>e.classList.remove('active'));
  document.querySelectorAll('.tabs .tab').forEach(e=>e.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  event.target.classList.add('active');
}}
function switchPool(pid){{
  document.querySelectorAll('.pool-section').forEach(e=>e.style.display='none');
  document.querySelectorAll('.pool-tab').forEach(e=>e.classList.remove('active'));
  var el=document.getElementById('pool-'+pid);
  if(el) el.style.display='block';
  var btn=document.getElementById('tab-'+pid);
  if(btn) btn.classList.add('active');
  curPool=pid;
}}
switchPool({first_pid});
let s=300;const cd=document.getElementById('cd');
setInterval(()=>{{s--;if(cd)cd.textContent=s>0?`${{Math.floor(s/60)}}:${{String(s%60).padStart(2,'0')}} 後刷新`:'刷新中...';if(s<=0)location.reload();}},1000);
function triggerScreen(){{
  fetch('/run/screen',{{method:'POST'}}).then(r=>r.json()).then(d=>alert('已觸發：'+JSON.stringify(d)));
}}
</script>
</body></html>"""
