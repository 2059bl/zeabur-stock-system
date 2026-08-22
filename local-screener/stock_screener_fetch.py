#!/usr/bin/env python3
"""
個股三合一篩選器資料抓取
涵蓋黃仁勳背板台股 52 家 + 補充名單，共 ~65 支
資料來源：FinMind（法人籌碼 + tick）+ TEJ（PE/PB 估值）
輸出：~/stock_screener_data.json
"""
import asyncio, json, os, sys, hashlib, urllib.request, urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from collections import defaultdict

# ── 優化5：Telegram 推播 ──────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = "8141519967:AAFodUHthSpQsFTN_4E4iUdm7tPEn_Sb9jE"
TELEGRAM_CHAT_ID   = "8745415790"

def tg_send(msg: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(url, data, timeout=10)
    except Exception as e:
        print(f"  ⚠ Telegram 推播失敗: {e}")

def check_signal_alerts(results: list):
    """
    個股自動警報 — 對照 XScript_Preset 警示條件移植
    觸發後依優先級分組，推播 Telegram
    """
    high, mid = [], []

    for s in results:
        code  = s["code"]
        name  = s["name"]
        close = s.get("close", 0)
        lines = []

        # ── 高優先：技術突破 ──────────────────────────────────────────
        if s.get("kd_golden_cross"):
            k = s.get("kd_k", 0); d = s.get("kd_d", 0)
            lines.append(f"  📈 KD黃金交叉  K={k} D={d}（低檔<50）")

        if s.get("macd_golden_cross"):
            dif = s.get("macd_dif", 0); sig = s.get("macd_signal", 0)
            lines.append(f"  📈 MACD黃金交叉  DIF={dif} Signal={sig}")

        if s.get("ma5_cross_up_ma10"):
            lines.append(f"  📈 MA5 黃金交叉 MA10（均線翻揚）")

        # ── 高優先：籌碼/軋空 ─────────────────────────────────────────
        if s.get("is_short_squeeze"):
            mi = s.get("margin_consec_inc", 0)
            sd = s.get("short_consec_dec", 0)
            sr = s.get("short_margin_ratio", 0)
            lines.append(f"  🔥 軋空信號  融資連增{mi}日 融券連減{sd}日  券資比{sr}%")

        # ── 高優先：基本面強度 ────────────────────────────────────────
        if s.get("is_three_rate_up") and (s.get("revenue_n_month_high") or 0) >= 100:
            avg = s.get("three_rate_avg_qoq", 0)
            nmh = s.get("revenue_n_month_high", 0)
            lines.append(f"  💎 三率三昇+{nmh}月營收新高  三率均QoQ={avg:+.1f}%")

        # ── 中優先：EPS 季成長 ────────────────────────────────────────
        cg = s.get("eps_consec_growth", 0) or 0
        if cg >= 3:
            eq = s.get("eps_q", 0); ea = s.get("eps_4q_avg", 0)
            lines.append(f"  📊 EPS連續{cg}季成長  最新季={eq} 近4季均={ea}")

        # ── 中優先：融資連增 ──────────────────────────────────────────
        mi = s.get("margin_consec_inc", 0) or 0
        if mi >= 5 and not s.get("is_short_squeeze"):
            lines.append(f"  💰 融資連續{mi}日增加（資金持續進場）")

        # ── 中優先：RSI 超賣反彈 ─────────────────────────────────────
        if s.get("rsi_oversold"):
            rsi = s.get("rsi", 0)
            lines.append(f"  🔔 RSI超賣反彈  RSI={rsi}（<30）")

        # ── 中優先：均線多頭排列 ─────────────────────────────────────
        if s.get("ma_bull_alignment"):
            m5  = s.get("ma5", 0)
            m10 = s.get("ma10", 0)
            m20 = s.get("ma20", 0)
            lines.append(f"  📐 均線多頭排列  MA5={m5} > MA10={m10} > MA20={m20}")

        if not lines:
            continue

        # 計算優先級（有高優先條件就歸入 high）
        high_keywords = ["KD黃金交叉", "MACD黃金交叉", "軋空信號", "三率三昇", "MA5 黃金交叉"]
        is_high = any(kw in ln for ln in lines for kw in high_keywords)

        entry = (
            f"【{code} {name}】 ${close}\n"
            + "\n".join(lines)
        )
        if is_high:
            high.append(entry)
        else:
            mid.append(entry)

    if not high and not mid:
        print("  ✅ 無新增警報")
        return

    now_str = datetime.now().strftime("%m/%d %H:%M")
    if high:
        msg = (
            f"🚨 <b>個股警報｜高優先 {now_str}</b>\n"
            f"{'─'*22}\n"
            + "\n\n".join(high[:10])  # 最多10則避免過長
        )
        tg_send(msg)
        print(f"  📲 高優先警報 {len(high)} 則")

    if mid:
        msg = (
            f"📋 <b>個股警報｜中優先 {now_str}</b>\n"
            f"{'─'*22}\n"
            + "\n\n".join(mid[:10])
        )
        tg_send(msg)
        print(f"  📲 中優先警報 {len(mid)} 則")


def check_holding_alerts(results: list):
    """檢查持倉觸發警示條件，推播 Telegram"""
    alerts = []
    for s in results:
        cost = s.get("holding_cost")
        shares = s.get("holding_shares")
        close = s.get("close", 0)
        if not cost or not close:
            continue
        pnl_pct = (close - cost) / cost * 100

        if pnl_pct <= -7.0:
            alerts.append(
                f"🔴 <b>止損警示</b> {s['code']} {s['name']}\n"
                f"   現價 {close} ｜成本 {cost} ｜損益 {pnl_pct:+.1f}%\n"
                f"   已跌破 7% 止損線（{cost*0.93:.1f}），請評估減碼"
            )

        margin = s.get("holding_margin")
        if margin:
            loan = cost * margin * 0.6
            maintain = (close * margin) / loan * 100 if loan else 999
            if maintain < 130:
                alerts.append(
                    f"🚨 <b>融資追繳警示</b> {s['code']} {s['name']}\n"
                    f"   現價 {close} ｜融資維持率 {maintain:.0f}%（危險：低於 130%）\n"
                    f"   請立即聯繫券商或補繳保證金"
                )
            elif maintain < 166:
                alerts.append(
                    f"⚠ <b>融資注意</b> {s['code']} {s['name']}\n"
                    f"   現價 {close} ｜融資維持率 {maintain:.0f}%（警戒：低於 166%）"
                )

        if s.get("is_flip") and s.get("flip_direction") == -1:
            alerts.append(
                f"⚡ <b>持倉翻空警示</b> {s['code']} {s['name']}\n"
                f"   三大法人同向翻空｜20日法人流 {s.get('flow20',0):+.0f} 張\n"
                f"   現價 {close}，損益 {pnl_pct:+.1f}%"
            )

    if alerts:
        header = f"📊 <b>持倉警示 {datetime.now().strftime('%m/%d %H:%M')}</b>\n{'─'*20}\n"
        tg_send(header + "\n\n".join(alerts))
        print(f"  📲 已推播 {len(alerts)} 則持倉警示")
    else:
        print("  ✅ 持倉無警示")

try:
    import httpx
except ImportError:
    print("請先安裝 httpx：pip3 install httpx"); sys.exit(1)

# ── 版本控制配置 ────────────────────────────────────────────────────────────────
VERSION = "1.0.0"
CHANGELOG = [
    {"date": "2026-08-12", "change": "Added version control and data hash verification"},
    {"date": "2026-08-05", "change": "Initial release with stock screening data"}
]

def calculate_data_hash(data):
    """計算數據完整性校驗值"""
    json_str = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(json_str.encode()).hexdigest()[:16]

# ── FinMind Token ──────────────────────────────────────────────────────────────
def load_token():
    t = os.environ.get("FINMIND_API_KEY") or os.environ.get("FINMIND_TOKEN","")
    if not t:
        f = Path.home() / ".tw_stock_token"
        if f.exists():
            for line in f.read_text().splitlines():
                if line.startswith("TOKEN="):
                    t = line[6:].strip(); break
    return t

TOKEN     = load_token()
FM_BASE   = "https://api.finmindtrade.com/api/v4/data"

# ── TEJ 設定 ──────────────────────────────────────────────────────────────────
TEJ_KEY  = "REln7q5OelpUFFGzxFy9y6KUyte8LS"
TEJ_BASE = "https://api.tej.com.tw"
TEJ_HDR  = {
    "accept": "application/json,application/octet-stream",
    "request-source": "python",
    "User-Agent": "TejApi Client/0.1.31",
    "x-api-token": TEJ_KEY,
}

# ── TEJ：一次拉所有股票的最新 PE/PB ────────────────────────────────────────────
def fetch_tej_valuations(codes: list, trade_date: str) -> dict:
    """
    回傳 {code: {pe_ratio, pb_ratio, cdiv_ratio}}
    單次 HTTP 請求抓取所有個股，效率極高。
    """
    result = {}
    try:
        coid_str = ",".join(codes)
        with httpx.Client(timeout=20, headers=TEJ_HDR) as c:
            r = c.get(f"{TEJ_BASE}/datatables/TWN/EWPRCD", params={
                "coid": coid_str,
                "mdate": trade_date,
                "opts.limit": len(codes) + 10,
            })
            if r.status_code != 200:
                print(f"  ⚠ TEJ [{r.status_code}]: {r.text[:100]}")
                return result
            d = r.json().get("datatable", {})
            cols = [col["name"] for col in d.get("columns", [])]
            for row in d.get("data", []):
                row_d = dict(zip(cols, row))
                code = row_d.get("coid", "")
                if code:
                    result[code] = {
                        "pe_ratio":   row_d.get("pe_ratio"),
                        "pb_ratio":   row_d.get("pb_ratio"),
                        "cdiv_ratio": row_d.get("cdiv_ratio"),
                    }
        print(f"  ✓ TEJ PE/PB：取得 {len(result)} 支（共請求 {len(codes)} 支）")
    except Exception as e:
        print(f"  ⚠ TEJ 請求失敗：{e}")
    return result

# ── 股票名單 ──────────────────────────────────────────────────────────────────
# 黃仁勳背板台股 52 家（原始名單）+ 7/6月營收創新高三率三昇特選 83 檔 + 補充名單
_T = ["三率三昇"]   # 7/6月營收創新高 + 毛利/營業利/淨利率三率同步上昇

STOCKS = {
    # ── AI 伺服器 / ODM ────────────────────────────────────────────────────
    "2301": {"name":"光寶科",   "group":"AI伺服器/ODM", "tags":_T},
    "2308": {"name":"台達電",   "group":"AI伺服器/ODM", "tags":_T},
    "2312": {"name":"金寶",     "group":"AI伺服器/ODM", "tags":_T},
    "2317": {"name":"鴻海",     "group":"AI伺服器/ODM"},
    "2324": {"name":"仁寶",     "group":"AI伺服器/ODM", "tags":_T},
    "2356": {"name":"英業達",   "group":"AI伺服器/ODM"},
    "2382": {"name":"廣達",     "group":"AI伺服器/ODM", "tags":_T},
    "3231": {"name":"緯創",     "group":"AI伺服器/ODM", "tags":_T},
    "3693": {"name":"營邦",     "group":"AI伺服器/ODM"},
    "4938": {"name":"和碩",     "group":"AI伺服器/ODM", "tags":_T},
    "6669": {"name":"緯穎",     "group":"AI伺服器/ODM"},
    "8210": {"name":"勤誠",     "group":"AI伺服器/ODM"},
    # ── 半導體 / 封測 / IC設計 ─────────────────────────────────────────────
    "2303": {"name":"聯電",     "group":"半導體/封測",  "tags":_T},
    "2330": {"name":"台積電",   "group":"半導體/封測"},
    "2360": {"name":"致茂",     "group":"半導體/封測",  "tags":_T},
    "2408": {"name":"南亞科",   "group":"半導體/封測",  "tags":_T},
    "2449": {"name":"京元電",   "group":"半導體/封測",  "tags":_T},
    "2454": {"name":"聯發科",   "group":"半導體/封測"},
    "3006": {"name":"晶豪科",   "group":"半導體/封測",  "tags":_T},
    "3105": {"name":"穩懋",     "group":"半導體/封測",  "tags":_T},
    "3264": {"name":"欣銓",     "group":"半導體/封測",  "tags":_T},
    "3711": {"name":"日月光",   "group":"半導體/封測",  "tags":_T},
    "5269": {"name":"祥碩",     "group":"半導體/封測",  "tags":_T},
    "6223": {"name":"旺矽",     "group":"半導體/封測",  "tags":_T},
    "6239": {"name":"力成",     "group":"半導體/封測",  "tags":_T},
    "6271": {"name":"同欣電",   "group":"半導體/封測",  "tags":_T},
    "6426": {"name":"統新",     "group":"半導體/封測",  "tags":_T},
    "6435": {"name":"精材",     "group":"半導體/封測",  "tags":_T},
    "6770": {"name":"力積電",   "group":"半導體/封測",  "tags":_T},
    # ── 散熱 / 電源 / 被動元件 ─────────────────────────────────────────────
    "2419": {"name":"仲琦",     "group":"散熱/電源"},
    "2481": {"name":"強茂",     "group":"散熱/電源",    "tags":_T},
    "3015": {"name":"全漢",     "group":"散熱/電源"},
    "3017": {"name":"奇鋐",     "group":"散熱/電源",    "tags":_T},
    "3324": {"name":"雙鴻",     "group":"散熱/電源"},
    "3653": {"name":"健策",     "group":"散熱/電源",    "tags":_T},
    "3665": {"name":"貿聯-KY",  "group":"散熱/電源",    "tags":_T},
    "4991": {"name":"環宇-KY",  "group":"散熱/電源",    "tags":_T},
    "6117": {"name":"迎廣",     "group":"散熱/電源"},
    "6173": {"name":"信昌電",   "group":"散熱/電源",    "tags":_T},
    "6449": {"name":"鈺邦",     "group":"散熱/電源",    "tags":_T},
    # ── 品牌 / 板卡 / 周邊 ─────────────────────────────────────────────────
    "1810": {"name":"和成",     "group":"品牌/板卡",    "tags":_T},
    "2353": {"name":"宏碁",     "group":"品牌/板卡",    "tags":_T},
    "2357": {"name":"華碩",     "group":"品牌/板卡",    "tags":_T},
    "2376": {"name":"技嘉",     "group":"品牌/板卡",    "tags":_T},
    "2377": {"name":"微星",     "group":"品牌/板卡",    "tags":_T},
    "2399": {"name":"映泰",     "group":"品牌/板卡",    "tags":_T},
    "2465": {"name":"麗臺",     "group":"品牌/板卡"},
    "2467": {"name":"志聖",     "group":"品牌/板卡"},
    "2480": {"name":"敦陽",     "group":"品牌/板卡"},
    "3008": {"name":"大立光",   "group":"品牌/板卡",    "tags":_T},
    "3029": {"name":"零壹",     "group":"品牌/板卡"},
    "3048": {"name":"益登",     "group":"品牌/板卡"},
    "3443": {"name":"創意",     "group":"品牌/板卡"},
    "3661": {"name":"世芯-KY",  "group":"品牌/板卡"},
    "5443": {"name":"均豪",     "group":"品牌/板卡"},
    "6125": {"name":"廣運",     "group":"品牌/板卡"},
    "6197": {"name":"佳必琪",   "group":"品牌/板卡"},
    "6206": {"name":"宏正",     "group":"品牌/板卡",    "tags":_T},
    "6227": {"name":"茂綸",     "group":"品牌/板卡"},
    "6640": {"name":"均華",     "group":"品牌/板卡"},
    "8064": {"name":"東捷",     "group":"品牌/板卡"},
    # ── CPO / 光通訊 ──────────────────────────────────────────────────────
    "3044": {"name":"健鼎",     "group":"CPO/光通訊"},
    "3081": {"name":"聯亞光",   "group":"CPO/光通訊"},
    "3163": {"name":"波若威",   "group":"CPO/光通訊"},
    "3363": {"name":"上詮",     "group":"CPO/光通訊"},
    "3441": {"name":"聯一光",   "group":"CPO/光通訊",   "tags":_T},
    "6226": {"name":"光鼎",     "group":"CPO/光通訊",   "tags":_T},
    "6238": {"name":"訊芯-KY",  "group":"CPO/光通訊"},
    "6743": {"name":"東典光電", "group":"CPO/光通訊",   "tags":_T},
    "7917": {"name":"源傑科技", "group":"CPO/光通訊",   "tags":_T},
    # ── PCB 高階板 / CCL ──────────────────────────────────────────────────
    "2368": {"name":"金像電",   "group":"PCB高階板",    "tags":_T},
    "2383": {"name":"台光電",   "group":"PCB高階板",    "tags":_T},
    "3037": {"name":"欣興",     "group":"PCB高階板",    "tags":_T},
    "3189": {"name":"景碩",     "group":"PCB高階板",    "tags":_T},
    "4541": {"name":"晟鈦",     "group":"PCB高階板",    "tags":_T},
    "4561": {"name":"亞電",     "group":"PCB高階板",    "tags":_T},
    "6187": {"name":"萬潤",     "group":"PCB高階板",    "tags":_T},
    "6213": {"name":"聯茂",     "group":"PCB高階板",    "tags":_T},
    "8039": {"name":"台虹",     "group":"PCB高階板",    "tags":_T},
    "8046": {"name":"南電",     "group":"PCB高階板",    "tags":_T},
    # ── 面板 / LED ────────────────────────────────────────────────────────
    "2409": {"name":"友達",     "group":"面板",         "tags":_T},
    "2426": {"name":"鼎元",     "group":"面板",         "tags":_T},
    "3498": {"name":"陽程",     "group":"面板",         "tags":_T},
    "3591": {"name":"艾笛森",   "group":"面板"},
    "3714": {"name":"富采",     "group":"面板",         "tags":_T},
    "6172": {"name":"宏齊",     "group":"面板",         "tags":_T},
    # ── 工業 AI / 邊緣運算 / 機器人 ────────────────────────────────────────
    "2305": {"name":"全友",     "group":"工業AI/邊緣",  "tags":_T},
    "2332": {"name":"友訊",     "group":"工業AI/邊緣",  "tags":_T},
    "2345": {"name":"智邦",     "group":"工業AI/邊緣",  "tags":_T},
    "2359": {"name":"所羅門",   "group":"工業AI/邊緣"},
    "2362": {"name":"藍天",     "group":"工業AI/邊緣"},
    "2392": {"name":"正崴",     "group":"工業AI/邊緣"},
    "2395": {"name":"研華",     "group":"工業AI/邊緣",  "tags":_T},
    "2417": {"name":"圓剛",     "group":"工業AI/邊緣"},
    "4585": {"name":"達明",     "group":"工業AI/邊緣"},
    "5289": {"name":"宜鼎",     "group":"工業AI/邊緣",  "tags":_T},
    "5474": {"name":"聰泰",     "group":"工業AI/邊緣"},
    "5484": {"name":"慧友",     "group":"工業AI/邊緣"},
    "6139": {"name":"亞翔",     "group":"工業AI/邊緣",  "tags":_T},
    "6166": {"name":"凌華",     "group":"工業AI/邊緣"},
    "6245": {"name":"立端",     "group":"工業AI/邊緣"},
    "6414": {"name":"樺漢",     "group":"工業AI/邊緣",  "tags":_T},
    "6579": {"name":"研揚",     "group":"工業AI/邊緣",  "tags":_T},
    "6922": {"name":"宸曜",     "group":"工業AI/邊緣"},
    "8171": {"name":"臺慶科",   "group":"工業AI/邊緣",  "tags":_T},
    "8234": {"name":"新漢",     "group":"工業AI/邊緣"},
    # ── 金融 ──────────────────────────────────────────────────────────────
    "2882": {"name":"國泰金",   "group":"金融"},
    "2884": {"name":"玉山金",   "group":"金融"},
    # ── 網通設備 ──────────────────────────────────────────────────────────
    "5388": {"name":"中磊",     "group":"網通設備",     "tags":_T},
    # ── 存儲 / Flash 控制 ─────────────────────────────────────────────────
    "5351": {"name":"鈺創",     "group":"存儲/Flash控制","tags":_T},
    "8299": {"name":"群聯",     "group":"存儲/Flash控制","tags":_T},
    # ── 持股追蹤（使用者實際持有）────────────────────────────────────────
    # cost=成本價, shares=股數, margin=融資張數
    "8358": {"name":"金居",     "group":"持股追蹤",     "cost":430.0,  "shares":1000},
    "6182": {"name":"合晶",     "group":"持股追蹤",     "cost":138.7,  "shares":3000, "tags":_T},
    "2303": {"name":"聯電",     "group":"持股追蹤",     "cost":128.2,  "shares":3000, "margin":3000, "tags":_T},
    "2363": {"name":"矽統",     "group":"持股追蹤"},
    "3663": {"name":"鑫科",     "group":"持股追蹤"},
    "6274": {"name":"台燿",     "group":"持股追蹤",     "tags":_T},
    "00403A": {"name":"主動統一台股增長", "group":"持股追蹤"},
    # ── 投顧推薦（健檢名單）──────────────────────────────────────────────
    "1303": {"name":"南亞",     "group":"投顧推薦",     "tags":_T},
    "2327": {"name":"國巨",     "group":"投顧推薦",     "tags":_T},
    "2344": {"name":"華邦電",   "group":"投顧推薦",     "tags":_T},
    "2455": {"name":"全新",     "group":"投顧推薦",     "tags":_T},
    "3374": {"name":"精材",     "group":"投顧推薦"},
    "4958": {"name":"臻鼎-KY",  "group":"投顧推薦",     "tags":_T},
    "5439": {"name":"高技",     "group":"投顧推薦",     "tags":_T},
    "6147": {"name":"頎邦",     "group":"投顧推薦",     "tags":_T},
    "6209": {"name":"今國光",   "group":"投顧推薦",     "tags":_T},
    "6488": {"name":"環球晶",   "group":"投顧推薦",     "tags":_T},
    "9958": {"name":"世紀鋼",   "group":"投顧推薦"},
    # ── 三維定位法驗證（驗證標的）──────────────────────────────────────────
    "3450": {"name":"聯鈞",     "group":"驗證標的"},
}

CHIP_NAMES = {
    "Foreign_Investor","Foreign_Dealer_Self","Investment_Trust",
    "Dealer_self","Dealer_Hedging",
    "外資","外資自營","外資及陸資(不含外資自營商)","外資自營商","投信","自營商","自營商(自行買賣)","自營商(避險)"
}
FOREIGN_NAMES = {"Foreign_Investor","Foreign_Dealer_Self","外資","外資自營","外資及陸資(不含外資自營商)","外資自營商"}
TRUST_NAMES   = {"Investment_Trust","投信"}
DEALER_NAMES  = {"Dealer_self","Dealer_Hedging","自營商","自營商(自行買賣)","自營商(避險)"}

# ── FinMind 請求 ───────────────────────────────────────────────────────────────
async def fm_get(sem, dataset, data_id, start, client):
    async with sem:
        try:
            r = await client.get(FM_BASE, params={
                "dataset":dataset,"data_id":data_id,"start_date":start,"token":TOKEN
            })
            body = r.json()
            return body.get("data",[]) if body.get("status")==200 else []
        except Exception as e:
            print(f"  ⚠ {dataset} {data_id}: {e}")
            return []

# ── 月營收分析 ────────────────────────────────────────────────────────────────
def calc_three_rate_metrics(fin_rows: list) -> dict:
    """
    fin_rows: TaiwanStockFinancialStatements 資料
    回傳: gross_margin_q (最新季), op_margin_q, net_margin_q,
          gross_margin_qoq, op_margin_qoq, net_margin_qoq, three_rate_avg_qoq, is_three_rate_up
    """
    if not fin_rows:
        return {}
    TYPES = {"Revenue", "GrossProfit", "OperatingIncome", "IncomeAfterTaxes"}
    by_date: dict = {}
    for row in fin_rows:
        t = row.get("type", "")
        if t not in TYPES:
            continue
        dt = row.get("date", "")
        by_date.setdefault(dt, {})[t] = row.get("value") or 0

    quarters = []
    for dt in sorted(by_date.keys()):
        v = by_date[dt]
        rev = v.get("Revenue") or 0
        if rev <= 0:
            continue
        quarters.append({
            "date":       dt,
            "gross":      round(v.get("GrossProfit", 0) / rev * 100, 2),
            "op":         round(v.get("OperatingIncome", 0) / rev * 100, 2),
            "net":        round(v.get("IncomeAfterTaxes", 0) / rev * 100, 2),
        })

    if len(quarters) < 2:
        return {}

    cur, prev = quarters[-1], quarters[-2]
    g_qoq = round(cur["gross"] - prev["gross"], 2)
    o_qoq = round(cur["op"]    - prev["op"],    2)
    n_qoq = round(cur["net"]   - prev["net"],   2)
    avg   = round((g_qoq + o_qoq + n_qoq) / 3, 2)
    return {
        "gross_margin_q":   cur["gross"],
        "op_margin_q":      cur["op"],
        "net_margin_q":     cur["net"],
        "gross_margin_qoq": g_qoq,
        "op_margin_qoq":    o_qoq,
        "net_margin_qoq":   n_qoq,
        "three_rate_avg_qoq": avg,
        "is_three_rate_up": g_qoq > 0 and o_qoq > 0 and n_qoq > 0,
        "fin_quarter":      cur["date"],
    }


def calc_eps_metrics(fin_rows: list) -> dict:
    """從 TaiwanStockFinancialStatements 計算 EPS 季成長指標"""
    eps_by_date: dict = {}
    for row in fin_rows:
        if row.get("type") == "EPS":
            dt = row.get("date", "")
            val = row.get("value")
            if dt and val is not None:
                try:
                    eps_by_date[dt] = float(val)
                except (ValueError, TypeError):
                    pass
    quarters = [(dt, eps_by_date[dt]) for dt in sorted(eps_by_date.keys())]
    if len(quarters) < 2:
        return {}
    eps_q = quarters[-1][1]
    eps_qoq = round(eps_q - quarters[-2][1], 2)
    # 連續成長季數
    consec = 0
    for i in range(len(quarters) - 1, 0, -1):
        if quarters[i][1] > quarters[i-1][1]:
            consec += 1
        else:
            break
    last4 = [q[1] for q in quarters[-4:]]
    return {
        "eps_q":            eps_q,
        "eps_qoq":          eps_qoq,
        "eps_consec_growth": consec,
        "eps_4q_avg":       round(sum(last4) / len(last4), 2),
        "eps_4q_sum":       round(sum(last4), 2),
        "is_eps_growing":   eps_qoq > 0,
    }


def calc_margin_metrics(margin_rows: list) -> dict:
    """從 TaiwanStockMarginPurchaseShortSale 計算融資融券指標"""
    if not margin_rows:
        return {}
    rows = sorted(margin_rows, key=lambda r: r.get("date", ""))
    recent = rows[-10:] if len(rows) >= 10 else rows
    if len(recent) < 2:
        return {}

    margin_bal = [float(r.get("MarginPurchaseToday") or 0) for r in recent]
    short_bal  = [float(r.get("ShortSaleToday") or 0) for r in recent]

    # 融資連續增加天數
    margin_consec = 0
    for i in range(len(margin_bal) - 1, 0, -1):
        if margin_bal[i] > margin_bal[i-1]:
            margin_consec += 1
        else:
            break

    # 融券連續減少天數
    short_consec = 0
    for i in range(len(short_bal) - 1, 0, -1):
        if short_bal[i] < short_bal[i-1]:
            short_consec += 1
        else:
            break

    last5_m = margin_bal[-5:] if len(margin_bal) >= 5 else margin_bal
    last5_s = short_bal[-5:]  if len(short_bal)  >= 5 else short_bal

    m_today = margin_bal[-1]
    s_today = short_bal[-1]
    short_margin_ratio = round(s_today / m_today * 100, 1) if m_today > 0 else 0

    return {
        "margin_consec_inc":   margin_consec,
        "short_consec_dec":    short_consec,
        "margin_5d_high":      m_today >= max(last5_m) if last5_m else False,
        "short_5d_low":        s_today <= min(last5_s) if last5_s else False,
        "short_margin_ratio":  short_margin_ratio,
        "is_short_squeeze":    short_consec >= 3 and margin_consec >= 3,
    }


def calc_technical_metrics(prices: list) -> dict:
    """從日線 OHLCV 計算 MA / KD / RSI / MACD 技術指標"""
    if len(prices) < 30:
        return {}

    closes = [float(p.get("close") or 0) for p in prices]
    highs  = [float(p.get("max")   or p.get("close") or 0) for p in prices]
    lows   = [float(p.get("min")   or p.get("close") or 0) for p in prices]
    n = len(closes)

    def sma(arr, period):
        return round(sum(arr[-period:]) / period, 2) if len(arr) >= period else None

    ma5  = sma(closes, 5)
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)

    # 均線多/空頭排列
    ma_bull = bool(ma5 and ma10 and ma20 and ma5 > ma10 > ma20)
    ma_bear = bool(ma5 and ma10 and ma20 and ma5 < ma10 < ma20)

    # MA5 黃金/死亡交叉 MA10（今日 vs 昨日）
    ma5_prev  = sma(closes[:-1], 5)
    ma10_prev = sma(closes[:-1], 10)
    ma5_cross_up   = bool(ma5 and ma10 and ma5_prev and ma10_prev and
                          ma5 > ma10 and ma5_prev <= ma10_prev)
    ma5_cross_down = bool(ma5 and ma10 and ma5_prev and ma10_prev and
                          ma5 < ma10 and ma5_prev >= ma10_prev)

    # ── KD (9日隨機指標) ───────────────────────────────────────────────
    K, D = 50.0, 50.0
    kd_prev_k, kd_prev_d = 50.0, 50.0
    for i in range(8, n):
        h9 = max(highs[i-8:i+1])
        l9 = min(lows[i-8:i+1])
        rsv = (closes[i] - l9) / (h9 - l9) * 100 if h9 != l9 else 50.0
        kd_prev_k, kd_prev_d = K, D
        K = K * 2/3 + rsv / 3
        D = D * 2/3 + K / 3

    kd_k = round(K, 1)
    kd_d = round(D, 1)
    kd_golden = kd_k > kd_d and kd_prev_k <= kd_prev_d and kd_d < 50
    kd_dead   = kd_k < kd_d and kd_prev_k >= kd_prev_d and kd_d > 50

    # ── RSI (14日 Wilder 平滑) ─────────────────────────────────────────
    rsi = None
    if n >= 15:
        diffs = [closes[i] - closes[i-1] for i in range(1, n)]
        avg_g = sum(max(d, 0) for d in diffs[:14]) / 14
        avg_l = sum(max(-d, 0) for d in diffs[:14]) / 14
        for d in diffs[14:]:
            avg_g = (avg_g * 13 + max(d, 0)) / 14
            avg_l = (avg_l * 13 + max(-d, 0)) / 14
        rs = avg_g / avg_l if avg_l > 0 else 100
        rsi = round(100 - 100 / (1 + rs), 1)

    # ── MACD (12/26/9 EMA) ─────────────────────────────────────────────
    macd_dif = macd_signal = macd_hist = None
    macd_golden = macd_dead = False
    if n >= 35:
        k12, k26, k9 = 2/13, 2/27, 2/10
        e12 = e26 = closes[0]
        dif_series = []
        for i, c in enumerate(closes):
            if i > 0:
                e12 = e12 * (1 - k12) + c * k12
                e26 = e26 * (1 - k26) + c * k26
            if i >= 25:
                dif_series.append(e12 - e26)
        if len(dif_series) >= 9:
            sig = dif_series[0]
            sig_prev = sig
            for d in dif_series[1:]:
                sig_prev = sig
                sig = sig * (1 - k9) + d * k9
            dif_now = dif_series[-1]
            dif_prev = dif_series[-2]
            macd_dif    = round(dif_now, 3)
            macd_signal = round(sig, 3)
            macd_hist   = round(dif_now - sig, 3)
            macd_golden = dif_now > sig and dif_prev <= sig_prev
            macd_dead   = dif_now < sig and dif_prev >= sig_prev

    close_now = closes[-1]
    return {
        "ma5":  ma5,  "ma10": ma10, "ma20": ma20, "ma60": ma60,
        "ma_bull_alignment":   ma_bull,
        "ma_bear_alignment":   ma_bear,
        "ma5_cross_up_ma10":   ma5_cross_up,
        "ma5_cross_down_ma10": ma5_cross_down,
        "price_above_ma20": bool(ma20 and close_now > ma20),
        "price_below_ma20": bool(ma20 and close_now < ma20),
        "kd_k": kd_k,  "kd_d": kd_d,
        "kd_golden_cross": kd_golden,
        "kd_dead_cross":   kd_dead,
        "rsi":            rsi,
        "rsi_oversold":   rsi is not None and rsi < 30,
        "rsi_overbought": rsi is not None and rsi > 70,
        "macd_dif":    macd_dif,
        "macd_signal": macd_signal,
        "macd_hist":   macd_hist,
        "macd_golden_cross": macd_golden,
        "macd_dead_cross":   macd_dead,
    }


def calc_revenue_metrics(rev_rows: list) -> dict:
    """
    rev_rows: TaiwanStockMonthRevenue 資料，依 revenue_year/revenue_month 排序
    回傳: revenue_yoy_pct, revenue_n_month_high, revenue_vs_estimate, revenue_tag
    """
    if not rev_rows:
        return {}

    # 排序：最新在前（revenue_year desc, revenue_month desc）
    rows = sorted(rev_rows, key=lambda r: (int(r.get("revenue_year", 0)), int(r.get("revenue_month", 0))), reverse=True)

    # 最新月 YoY%（FinMind 不提供 revenue_year_growth，自行從原始月份數據計算）
    latest = rows[0]
    yoy_pct = None
    try:
        latest_month = int(latest.get("revenue_month", 0))
        latest_year  = int(latest.get("revenue_year", 0))
        latest_rev_v = float(latest.get("revenue", 0) or 0)
        # 找同月去年資料
        same_month_prev_year = next(
            (r for r in rows if int(r.get("revenue_year", 0)) == latest_year - 1
             and int(r.get("revenue_month", 0)) == latest_month),
            None
        )
        if same_month_prev_year and latest_rev_v > 0:
            prev_rev_v = float(same_month_prev_year.get("revenue", 0) or 0)
            if prev_rev_v > 0:
                yoy_pct = round((latest_rev_v - prev_rev_v) / prev_rev_v * 100, 1)
    except (ValueError, TypeError):
        pass

    # 月營收 N 月新高（往前找幾個月沒比現在高）
    latest_rev = None
    try:
        latest_rev = float(latest.get("revenue", 0) or 0)
    except (ValueError, TypeError):
        pass

    n_month_high = None
    if latest_rev and latest_rev > 0 and len(rows) > 1:
        n = 0
        for r in rows[1:]:
            try:
                v = float(r.get("revenue", 0) or 0)
            except (ValueError, TypeError):
                v = 0
            if v < latest_rev:
                n += 1
            else:
                break
        n_month_high = n if n > 0 else None  # None 表示非新高

    # 月營收 vs 市場預期（用前 3 個月各自的 YoY 均值為「預期」，與最新 YoY 比較）
    revenue_vs_estimate = None
    estimate_tag = None
    yoy_history = []
    for r in rows[1:4]:  # 前 3 個月
        try:
            r_month = int(r.get("revenue_month", 0))
            r_year  = int(r.get("revenue_year", 0))
            r_rev   = float(r.get("revenue", 0) or 0)
            prev_r  = next(
                (x for x in rows if int(x.get("revenue_year", 0)) == r_year - 1
                 and int(x.get("revenue_month", 0)) == r_month),
                None
            )
            if prev_r and r_rev > 0:
                prev_r_rev = float(prev_r.get("revenue", 0) or 0)
                if prev_r_rev > 0:
                    yoy_history.append((r_rev - prev_r_rev) / prev_r_rev * 100)
        except (ValueError, TypeError):
            pass

    if yoy_pct is not None and len(yoy_history) >= 2:
        expected = sum(yoy_history) / len(yoy_history)
        diff = yoy_pct - expected
        revenue_vs_estimate = round(diff, 1)
        if diff >= 15:
            estimate_tag = f"遠高於預期+{diff:.1f}%"
        elif diff >= -5:
            estimate_tag = f"符合預期{diff:+.1f}%"
        else:
            estimate_tag = f"低於預期{diff:.1f}%"

    return {
        "revenue_yoy_pct":       yoy_pct,
        "revenue_n_month_high":  n_month_high,
        "revenue_vs_estimate":   revenue_vs_estimate,
        "revenue_estimate_tag":  estimate_tag,
    }


# ── 計算個股指標 ──────────────────────────────────────────────────────────────
def calc_stock_metrics(code, meta, chip_rows, price_rows, tick_rows, trading_dates, tej_val=None, gov_rows=None, rev_rows=None, fin_metrics=None, margin_metrics=None, eps_metrics=None):
    # ── 法人逐日淨買超 ─────────────────────────────────────────────────────
    foreign_daily = defaultdict(int)
    trust_daily   = defaultdict(int)
    dealer_daily  = defaultdict(int)
    for r in chip_rows:
        d = r.get("date",""); name = r.get("name","")
        net = int(r.get("buy") or 0) - int(r.get("sell") or 0)
        if name in FOREIGN_NAMES: foreign_daily[d] += net
        elif name in TRUST_NAMES: trust_daily[d]   += net
        elif name in DEALER_NAMES: dealer_daily[d] += net

    # 官股（政府銀行）逐日淨買超
    gov_daily = defaultdict(int)
    for r in (gov_rows or []):
        d = r.get("date","")
        net = int(r.get("buy") or 0) - int(r.get("sell") or 0)
        gov_daily[d] += net

    # flow_1y = 三大法人合計一年期累積淨買超（張）
    # flow20 = 三大法人合計近 20 日累積淨買超（張）用於短期籌碼判斷
    all_daily = defaultdict(int)
    for d in set(list(foreign_daily) + list(trust_daily) + list(dealer_daily)):
        all_daily[d] = foreign_daily[d] + trust_daily[d] + dealer_daily[d]

    vals   = [all_daily.get(d, 0) / 1000 for d in trading_dates]  # 張
    flow_1y = round(sum(vals), 0)  # 一年累積淨買超
    flow20 = round(sum(vals[:20]), 0) if len(vals) >= 20 else round(sum(vals), 0)  # 20日累積淨買超

    last5  = sum(vals[:5]) / 5 if vals[:5] else 0
    prev5  = sum(vals[5:10]) / 5 if vals[5:10] else 0
    accel_5d = round(last5 - prev5, 1)  # 近 5 日加速度
    accel5 = accel_5d  # 保留舊名稱用於投顧健檢

    # 外資連買/連賣天數
    consec = 0
    if trading_dates:
        sign = None
        for d in trading_dates:
            v = foreign_daily.get(d, 0)
            s = 1 if v > 0 else (-1 if v < 0 else 0)
            if s == 0: break
            if sign is None: sign = s
            if s == sign: consec += s
            else: break

    # ── 價格計算 ──────────────────────────────────────────────────────────
    prices = sorted([r for r in price_rows if "close" in r], key=lambda x: x["date"])
    change_1y = 0.0; change5 = 0.0; change1 = 0.0; close_price = 0.0; open_price = 0.0
    change_4d = 0.0  # 7/30 以來的 4 日漲幅
    price_730 = 0.0  # 7/30 收盤價

    if len(prices) >= 2:
        close_price = float(prices[-1].get("close", 0))
        open_price  = float(prices[-1].get("open", close_price))
        change1 = round((close_price - float(prices[-2]["close"])) / float(prices[-2]["close"]) * 100, 2) if prices[-2]["close"] else 0

    # 查找 7/30 的收盤價（強勢反彈基準日）
    for p in prices:
        if p.get("date", "") == "2026-07-30":
            price_730 = float(p.get("close", 0))
            break

    # 計算 7/30-今日的漲幅（強勢反彈判定）
    if price_730 > 0 and close_price > 0:
        change_4d = round((close_price - price_730) / price_730 * 100, 2)

    # 計算一年期漲跌幅（從最早的交易日到現在）
    if len(prices) >= 2:
        base = float(prices[0]["close"])
        change_1y = round((close_price - base) / base * 100, 2) if base else 0

    # 計算 5 日漲跌幅（用於投顧健檢）
    if len(prices) >= 6:
        base5 = float(prices[-6]["close"])
        change5 = round((close_price - base5) / base5 * 100, 2) if base5 else 0

    # ── 尾盤動能（12:30 後主動買超）─────────────────────────────────────
    tail_buy = tail_sell = all_buy = all_sell = 0
    for r in tick_rows:
        vol  = int(r.get("volume") or 0)
        tick = str(r.get("TickType",""))
        t    = r.get("Time","")
        if tick == "1": all_buy  += vol
        elif tick == "2": all_sell += vol
        if t >= "12:30":
            if tick == "1": tail_buy  += vol
            elif tick == "2": tail_sell += vol

    tail_net   = tail_buy - tail_sell
    tail_ratio = round(tail_buy / (all_buy + 1) * 100, 1)  # 尾盤主動買%
    day_net    = all_buy - all_sell

    # ── 法人當日方向（最新 2 日）─────────────────────────────────────────
    today_dir    = {}
    yesterday_dir = {}
    if len(trading_dates) >= 2:
        d0, d1 = trading_dates[0], trading_dates[1]
        for label, daily in [("foreign",foreign_daily),("trust",trust_daily),("dealer",dealer_daily)]:
            today_dir[label]     = 1 if daily.get(d0,0) > 0 else (-1 if daily.get(d0,0) < 0 else 0)
            yesterday_dir[label] = 1 if daily.get(d1,0) > 0 else (-1 if daily.get(d1,0) < 0 else 0)

    # 三方同向判定
    td = list(today_dir.values())
    yd = list(yesterday_dir.values())
    all_same_today = all(v == td[0] and v != 0 for v in td)
    all_same_yday  = all(v == yd[0] and v != 0 for v in yd) if yd else False
    direction_flip = all_same_today and (not all_same_yday or td[0] != yd[0])

    # ── TEJ 估值資料 ─────────────────────────────────────────────────────
    pe   = tej_val.get("pe_ratio")   if tej_val else None
    pb   = tej_val.get("pb_ratio")   if tej_val else None
    cdiv = tej_val.get("cdiv_ratio") if tej_val else None

    # 評價 tag（依本益比）
    valuation_tag = None
    if pe is not None:
        try:
            pe_f = float(pe)
            if pe_f > 30:
                valuation_tag = "昂貴"
            elif pe_f > 20:
                valuation_tag = "合理偏高"
            elif pe_f > 10:
                valuation_tag = "合理偏低"
            else:
                valuation_tag = "便宜"
        except (ValueError, TypeError):
            pass

    # 體質 tag（依三率 + 月營收成長）
    tags_list = meta.get("tags", [])
    has_three_rate = "三率三昇" in tags_list
    # 簡單以 flow20 + change_1y 判斷活躍度（財報需 /three-rate 端點才有，這裡用代理指標）
    if has_three_rate and flow20 > 0:
        quality_tag = "優良"
    elif has_three_rate or flow20 > 0:
        quality_tag = "普通"
    else:
        quality_tag = "注意"

    # 月營收指標
    rev_metrics = calc_revenue_metrics(rev_rows or [])

    # 技術指標（從現有日線計算，不需額外 API）
    tech = calc_technical_metrics(prices)

    # 委屈股：近 20 日法人淨買超 > 0（短期持續進場）且 5 日股價跌 < -2%（股價仍受壓）
    # 使用 20 日指標，避免年度多頭行情導致 change_1y 永遠正值而篩不出結果
    base_frustrated = (flow20 > 0 and change5 < -2.0)

    # ── 一年籌碼圖資料（由舊到新）────────────────────────────────────────
    dates_asc = list(reversed(trading_dates))  # oldest → newest
    f_vals   = [foreign_daily.get(d, 0) // 1000 for d in dates_asc]  # 張
    gov_vals = [gov_daily.get(d, 0) // 1000     for d in dates_asc]
    tot_vals = [all_daily.get(d, 0) // 1000     for d in dates_asc]  # 三大法人合計

    def cumsum(lst):
        s, out = 0, []
        for v in lst:
            s += v; out.append(s)
        return out

    chip_1y = {
        "dates":       dates_asc,
        "foreign":     f_vals,
        "gov":         gov_vals,
        "total":       tot_vals,
        "cum_foreign": cumsum(f_vals),
        "cum_gov":     cumsum(gov_vals),
        "cum_total":   cumsum(tot_vals),
    }

    # ── 三維定位法計算 ──────────────────────────────────────────────────
    # 【第一維度】成本乖離率：(收盤 - 均價) / 均價 × 100%
    estimated_avg = (close_price + open_price) / 2 if close_price > 0 else close_price
    cost_bias = (close_price - estimated_avg) / estimated_avg * 100 if estimated_avg > 0 else 0

    # 【第三維度】多空平衡點：(最高 + 最低 + 收盤) / 3
    # 使用 FinMind API 的真實日高低價（max/min）
    daily_high = float(prices[-1].get("max", close_price)) if prices else close_price
    daily_low  = float(prices[-1].get("min", close_price)) if prices else close_price
    balance_point = (daily_high + daily_low + close_price) / 3 if daily_high > 0 and daily_low > 0 else 0
    balance_point = round(balance_point, 2)

    # 日線形態：收盤位置 (收盤-最低) / (最高-最低) × 100%
    daily_range = daily_high - daily_low
    close_position = (close_price - daily_low) / daily_range * 100 if daily_range > 0 else 50

    # ── 【第四維度】多層驗證機制 ──────────────────────────────────────
    # 倒貨訊號確認等級（0-4）
    confirm_count = 0
    if cost_bias < -3:       confirm_count += 1  # 驗證 1：成本乖離率 < -3%
    if consec < -2:          confirm_count += 1  # 驗證 2：外資連賣 2+ 天
    if tail_ratio > 60:      confirm_count += 1  # 驗證 3：尾盤主動售 > 60%
    if accel_5d > 500:       confirm_count += 1  # 驗證 4：量能加速 > 500 張/天

    # 倒貨風險等級判定
    if confirm_count >= 3:
        harvest_risk = "🔴 確認倒貨"  # 3-4 層驗證確認
    elif confirm_count >= 2:
        harvest_risk = "🟠 高度風險"  # 2 層驗證
    elif cost_bias < -3:
        harvest_risk = "🟡 中等風險"  # 僅有成本乖離
    else:
        harvest_risk = "🟢 低風險"

    # ── 逆向訊號：多頭反撲確認 ──────────────────────────────────────
    # 條件：收盤 > 平衡點 AND 外資連買 2+ 天 AND 尾盤主動買 > 70%
    is_bull_reversal = (close_price > balance_point and
                        consec > 1 and
                        (100 - tail_ratio) > 70 if tail_ratio > 0 else False)

    # ── 轉強篩選池判定 ──────────────────────────────────────────────────────────
    # 條件 A：7/30-今日漲幅 ≥ 30% 或 5 日漲幅 > 5%
    # 條件 B：法人淨買 > 0（近20日）
    # 條件 C：5日加速度 > 0（近期動能轉正）
    # 條件 D：技術面向上（close > balance_point）
    strong_rally_a = (change_4d >= 30.0) or (change5 > 5.0)
    strong_rally_b = (flow20 > 0)
    strong_rally_c = (accel5 > 0)
    strong_rally_d = (close_price > balance_point)

    strong_rally_count = sum([strong_rally_a, strong_rally_b, strong_rally_c, strong_rally_d])
    if strong_rally_count >= 3:
        strong_rally_signal = "★★★ 超強勢"  # 3-4 條件符合
    elif strong_rally_count >= 2:
        strong_rally_signal = "★★ 強勢"      # 2 條件符合
    elif strong_rally_a or (strong_rally_b and strong_rally_c):
        strong_rally_signal = "★ 轉強"       # 滿足主要條件
    else:
        strong_rally_signal = ""             # 不符合

    return {
        "code":    code,
        "name":    meta["name"],
        "group":   meta["group"],
        # 一年期指标（長期趨勢分析）
        "flow_1y": flow_1y,
        "accel_5d": accel_5d,
        "change_1y": change_1y,
        # 20 日指标（短期籌碼判斷 + 投顧健檢）
        "flow20": flow20,
        "accel5": accel5,
        "change5": change5,
        "change1": change1,
        "change_4d": change_4d,        # 7/30-今日漲幅（強勢反彈指標）
        "price_730": price_730,        # 7/30 收盤價
        "close":   close_price,
        "open":    open_price,
        # ── 日線數據 ──
        "high":    round(daily_high, 2),
        "low":     round(daily_low, 2),
        # ── 三維定位法 ──
        "cost_bias":       round(cost_bias, 2),          # 成本乖離率
        "estimated_avg":   round(estimated_avg, 2),      # 估計均價
        "balance_point":   balance_point,                # 多空平衡點（真實計算）
        "close_position":  round(close_position, 1),     # 收盤位置 0-100%
        "harvest_risk":    harvest_risk,                 # 倒貨風險等級
        "confirm_count":   confirm_count,                # 倒貨驗證層數
        "is_bull_reversal": is_bull_reversal,           # 多頭反撲訊號
        # ── 轉強篩選池 ──
        "strong_rally_signal": strong_rally_signal,     # 轉強等級
        "strong_rally_count": strong_rally_count,       # 符合條件數
        "consec_foreign": consec,
        # ── TEJ 估值 ──
        "pe_ratio":   pe,
        "pb_ratio":   pb,
        "cdiv_ratio": cdiv,
        # 委屈股信號（pe_ratio/pb_ratio 在 HTML 端做宇宙百分位）
        "is_frustrated": base_frustrated,
        # 3大法人同向翻多/翻空
        "is_flip": direction_flip,
        "flip_direction": td[0] if all_same_today else 0,
        "today_dir": today_dir,
        "yesterday_dir": yesterday_dir,
        # 尾盤動能
        "tail_net":   tail_net,
        "tail_ratio": tail_ratio,
        "day_net":    day_net,
        "is_tail_momentum": (
            tail_net > 0
            and tail_ratio >= 20
            and close_price >= open_price * 0.995
            and day_net > 0
        ),
        # "chip_1y": chip_1y,  # 全年資料，改用 chip_20d 節省 JSON 大小
        "chip_20d": {
            "dates":       dates_asc[-20:],
            "foreign":     f_vals[-20:],
            "gov":         gov_vals[-20:],
            "total":       tot_vals[-20:],
            "cum_foreign": cumsum(f_vals[-20:]),
            "cum_gov":     cumsum(gov_vals[-20:]),
            "cum_total":   cumsum(tot_vals[-20:]),
        },
        "tags": meta.get("tags", []),   # 例如 ["三率三昇"]
        # ── 評價 / 體質 ──
        "valuation_tag": valuation_tag,    # 昂貴/合理偏高/合理偏低/便宜
        "quality_tag":   quality_tag,      # 優良/普通/注意
        # ── 月營收 ──
        "revenue_yoy_pct":      rev_metrics.get("revenue_yoy_pct"),
        "revenue_n_month_high": rev_metrics.get("revenue_n_month_high"),
        "revenue_vs_estimate":  rev_metrics.get("revenue_vs_estimate"),
        "revenue_estimate_tag": rev_metrics.get("revenue_estimate_tag"),
        # 持倉資訊（只有 持股追蹤 組有）
        "holding_cost":   meta.get("cost"),
        "holding_shares": meta.get("shares"),
        "holding_margin": meta.get("margin"),
        # 三率 QoQ（季財報）
        "gross_margin_q":     (fin_metrics or {}).get("gross_margin_q"),
        "op_margin_q":        (fin_metrics or {}).get("op_margin_q"),
        "net_margin_q":       (fin_metrics or {}).get("net_margin_q"),
        "gross_margin_qoq":   (fin_metrics or {}).get("gross_margin_qoq"),
        "op_margin_qoq":      (fin_metrics or {}).get("op_margin_qoq"),
        "net_margin_qoq":     (fin_metrics or {}).get("net_margin_qoq"),
        "three_rate_avg_qoq": (fin_metrics or {}).get("three_rate_avg_qoq"),
        "is_three_rate_up":   (fin_metrics or {}).get("is_three_rate_up", False),
        "fin_quarter":        (fin_metrics or {}).get("fin_quarter"),
        # ── EPS 季成長 ──
        "eps_q":              (eps_metrics or {}).get("eps_q"),
        "eps_qoq":            (eps_metrics or {}).get("eps_qoq"),
        "eps_consec_growth":  (eps_metrics or {}).get("eps_consec_growth"),
        "eps_4q_avg":         (eps_metrics or {}).get("eps_4q_avg"),
        "eps_4q_sum":         (eps_metrics or {}).get("eps_4q_sum"),
        "is_eps_growing":     (eps_metrics or {}).get("is_eps_growing", False),
        # ── 融資融券 ──
        "margin_consec_inc":  (margin_metrics or {}).get("margin_consec_inc"),
        "short_consec_dec":   (margin_metrics or {}).get("short_consec_dec"),
        "margin_5d_high":     (margin_metrics or {}).get("margin_5d_high", False),
        "short_5d_low":       (margin_metrics or {}).get("short_5d_low", False),
        "short_margin_ratio": (margin_metrics or {}).get("short_margin_ratio"),
        "is_short_squeeze":   (margin_metrics or {}).get("is_short_squeeze", False),
        # ── 技術指標 ──
        "ma5":  tech.get("ma5"),  "ma10": tech.get("ma10"),
        "ma20": tech.get("ma20"), "ma60": tech.get("ma60"),
        "ma_bull_alignment":   tech.get("ma_bull_alignment", False),
        "ma_bear_alignment":   tech.get("ma_bear_alignment", False),
        "ma5_cross_up_ma10":   tech.get("ma5_cross_up_ma10", False),
        "ma5_cross_down_ma10": tech.get("ma5_cross_down_ma10", False),
        "price_above_ma20":    tech.get("price_above_ma20", False),
        "price_below_ma20":    tech.get("price_below_ma20", False),
        "kd_k": tech.get("kd_k"), "kd_d": tech.get("kd_d"),
        "kd_golden_cross":     tech.get("kd_golden_cross", False),
        "kd_dead_cross":       tech.get("kd_dead_cross", False),
        "rsi":                 tech.get("rsi"),
        "rsi_oversold":        tech.get("rsi_oversold", False),
        "rsi_overbought":      tech.get("rsi_overbought", False),
        "macd_dif":            tech.get("macd_dif"),
        "macd_signal":         tech.get("macd_signal"),
        "macd_hist":           tech.get("macd_hist"),
        "macd_golden_cross":   tech.get("macd_golden_cross", False),
        "macd_dead_cross":     tech.get("macd_dead_cross", False),
    }

# ── 主程式 ────────────────────────────────────────────────────────────────────
async def main():
    if not TOKEN:
        print("❌ 找不到 FinMind token"); sys.exit(1)

    today = date.today()
    start = (today - timedelta(days=365)).isoformat()  # 改為一年期
    codes = list(STOCKS.keys())
    print(f"📡 抓取 {len(codes)} 支個股資料（起始 {start}）")
    print(f"💡 時間範圍：一年期 (~250 個交易日)\n")

    async with httpx.AsyncClient(timeout=30) as client:
        sem = asyncio.Semaphore(6)

        # 先取交易日曆
        cal = await fm_get(sem, "TaiwanStockPrice", "0050", start, client)
        trading_dates = sorted(
            {r["date"] for r in cal if r["date"] <= today.isoformat()}, reverse=True
        )[:260]  # 一年約 250-260 個交易日
        print(f"📅 交易日期間：{trading_dates[0]} ～ {trading_dates[-1]}（共 {len(trading_dates)} 個交易日）\n")

        # Phase 1: 日線 + 法人（所有個股）
        print("📊 Phase 1：抓取法人籌碼 + 價格日線 + 官股…")
        async def fetch_daily(code):
            chip   = await fm_get(sem, "TaiwanStockInstitutionalInvestorsBuySell", code, start, client)
            prices = await fm_get(sem, "TaiwanStockPrice", code, start, client)
            gov    = await fm_get(sem, "TaiwanStockGovernmentBankBuySell", code, start, client)
            return code, chip, prices, gov

        daily_results = await asyncio.gather(*[fetch_daily(c) for c in codes])
        print(f"  ✓ 日線資料完成 {len(daily_results)} 支")

        # 預篩：只對有潛力的股票抓 tick（節省時間）
        pre_filter = {}
        today_str = trading_dates[0] if trading_dates else today.isoformat()
        for code, chip, prices, gov in daily_results:
            # 快速估算 flow_1y 決定是否需要 tick
            foreign_d = defaultdict(int)
            all_d = defaultdict(int)
            for r in chip:
                d = r.get("date",""); name = r.get("name","")
                net = int(r.get("buy") or 0) - int(r.get("sell") or 0)
                if name in FOREIGN_NAMES: foreign_d[d] += net
                all_d[d] += net if name in CHIP_NAMES else 0
            vals   = [all_d.get(d, 0) / 1000 for d in trading_dates]
            flow_1y = sum(vals)
            # 需要 tick：委屈股候選 OR 法人轉向候選
            pre_filter[code] = {"chip": chip, "prices": prices,
                                 "gov": gov,
                             "need_tick": True}  # 全部抓 tick 以支援尾盤動能

        # Phase 2: tick 資料（最新交易日）
        print(f"\n⚡ Phase 2：抓取尾盤 tick 資料（{today_str}）…")
        tick_needed = [c for c, v in pre_filter.items() if v["need_tick"]]

        async def fetch_tick(code):
            rows = await fm_get(sem, "TaiwanStockPriceTick", code, today_str, client)
            return code, rows

        tick_results = dict(await asyncio.gather(*[fetch_tick(c) for c in tick_needed]))
        has_tick = sum(1 for v in tick_results.values() if v)
        print(f"  ✓ tick 資料完成（{has_tick}/{len(tick_needed)} 支有資料）")

        # Phase 3.5: 月營收（120 個月/10年，計算 YoY / N月新高 / 市場預期）
        # 需要 10 年資料才能計算「100個月新高」
        print("\n📅 Phase 3.5：月營收資料（10年）…")
        rev_start = (today - timedelta(days=3650)).isoformat()

        async def fetch_rev(code):
            rows = await fm_get(sem, "TaiwanStockMonthRevenue", code, rev_start, client)
            return code, rows

        rev_results = dict(await asyncio.gather(*[fetch_rev(c) for c in codes]))
        print(f"  ✓ 月營收完成（{sum(1 for v in rev_results.values() if v)}/{len(codes)} 支有資料）")

        # Phase 3.6: 季財報（三率 QoQ）
        print("\n📊 Phase 3.6：季財報三率 QoQ…")
        fin_start = (today - timedelta(days=800)).isoformat()  # 8 季

        async def fetch_fin(code):
            rows = await fm_get(sem, "TaiwanStockFinancialStatements", code, fin_start, client)
            return code, rows

        fin_results = dict(await asyncio.gather(*[fetch_fin(c) for c in codes]))
        print(f"  ✓ 季財報完成（{sum(1 for v in fin_results.values() if v)}/{len(codes)} 支有資料）")

        # Phase 3.7: 融資融券（近10日，軋空/資增判定）
        print("\n💰 Phase 3.7：融資融券資料（近10日）…")
        margin_start = (today - timedelta(days=20)).isoformat()

        async def fetch_margin(code):
            rows = await fm_get(sem, "TaiwanStockMarginPurchaseShortSale", code, margin_start, client)
            return code, rows

        margin_results = dict(await asyncio.gather(*[fetch_margin(c) for c in codes]))
        print(f"  ✓ 融資融券完成（{sum(1 for v in margin_results.values() if v)}/{len(codes)} 支有資料）")

    # Phase 3: TEJ PE/PB（同步，單次請求）
    print(f"\n📈 Phase 3：TEJ 估值資料（PE/PB）…")
    tej_data = fetch_tej_valuations(codes, today_str)
    print()

    # ── 計算最終指標 ──────────────────────────────────────────────────────
    print("🧮 計算篩選指標…")
    results = []
    for code, chip, prices, gov in daily_results:
        tick = tick_results.get(code, [])
        meta = STOCKS[code]
        try:
            fin_rows    = fin_results.get(code, [])
            fin_metrics = calc_three_rate_metrics(fin_rows)
            eps_metrics = calc_eps_metrics(fin_rows)
            margin_metrics = calc_margin_metrics(margin_results.get(code, []))
            r = calc_stock_metrics(code, meta, chip, prices, tick, trading_dates,
                                   tej_val=tej_data.get(code),
                                   gov_rows=gov,
                                   rev_rows=rev_results.get(code, []),
                                   fin_metrics=fin_metrics,
                                   margin_metrics=margin_metrics,
                                   eps_metrics=eps_metrics)
            results.append(r)
            flags = []
            if r["is_frustrated"]: flags.append("委屈")
            if r["is_flip"]: flags.append("轉相↑" if r["flip_direction"]>0 else "轉相↓")
            if r["is_tail_momentum"]: flags.append("尾盤↑")
            tag = f"  [{' '.join(flags)}]" if flags else ""
            print(f"  {code} {meta['name']:8s}  flow_1y={r['flow_1y']:+.0f}  change_1y={r['change_1y']:+.1f}%  tail_net={r['tail_net']:+d}{tag}")
        except Exception as e:
            print(f"  ❌ {code} {meta['name']}: {e}")

    # ── 篩選結果摘要 ─────────────────────────────────────────────────────
    frustrated   = [r for r in results if r["is_frustrated"]]
    flips_bull   = [r for r in results if r["is_flip"] and r["flip_direction"]>0]
    flips_bear   = [r for r in results if r["is_flip"] and r["flip_direction"]<0]
    tail_momo    = [r for r in results if r["is_tail_momentum"]]

    print(f"\n📋 篩選結果：")
    print(f"  委屈股：{len(frustrated)} 支")
    print(f"  法人三向翻多：{len(flips_bull)} 支 / 翻空：{len(flips_bear)} 支")
    print(f"  尾盤動能：{len(tail_momo)} 支")

    now = datetime.now()
    # P2.1 版本控制
    data_payload = {
        "updated":    today.isoformat(),
        "timestamp":  now.isoformat(),
        "trade_date": trading_dates[0] if trading_dates else today.isoformat(),
        "count":      len(results),
        "stocks":     results,
    }
    out = {
        "version": VERSION,
        "timestamp": now.isoformat(),
        "dataHash": calculate_data_hash(data_payload),
        "changelog": CHANGELOG,
        "data": data_payload
    }
    out_path = Path.home() / "stock_screener_data.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # 顯示完整時間戳
    ts_display = now.strftime("%H:%M:%S")
    print(f"\n✅ 完成！已儲存至 {out_path}")
    print(f"   資料採樣時間：{today.isoformat()} {ts_display}")

    # 個股自動警報（XScript_Preset 條件移植）
    print("\n🔔 檢查個股警報…")
    check_signal_alerts(results)

    # 優化5：持倉 Telegram 警示
    print("\n📲 檢查持倉警示…")
    check_holding_alerts(results)

if __name__ == "__main__":
    asyncio.run(main())
