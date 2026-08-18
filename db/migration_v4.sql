-- ============================================================
-- Migration v4 — 系統重構 2026-08-18
-- 原則：不讓系統負擔崩潰
-- 新增：broker_daily, gov_bank_daily, api_usage_log
-- 合併：institutional_daily → stock_indicators
-- ============================================================

-- ── 券商分點日資料表（Task B 核心）────────────────────────────────────────────
-- 只在暴跌日（大盤跌幅 > 2%）才寫入，30天後自動清理
CREATE TABLE IF NOT EXISTS broker_daily (
    id                  BIGSERIAL PRIMARY KEY,
    stock_code          VARCHAR(10)  NOT NULL,
    trade_date          DATE         NOT NULL,
    securities_trader   VARCHAR(50)  NOT NULL,
    trader_id           VARCHAR(10),
    buy_shares          BIGINT       DEFAULT 0,
    sell_shares         BIGINT       DEFAULT 0,
    net_shares          BIGINT       GENERATED ALWAYS AS (buy_shares - sell_shares) STORED,
    avg_price           NUMERIC(10,2),
    is_foreign          BOOLEAN      DEFAULT FALSE,  -- 外資券商（高盛/美林/摩根等）
    is_gov_bank         BOOLEAN      DEFAULT FALSE,  -- 八大公股
    created_at          TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE(stock_code, trade_date, securities_trader)
);

CREATE INDEX IF NOT EXISTS idx_broker_daily_date     ON broker_daily(trade_date);
CREATE INDEX IF NOT EXISTS idx_broker_daily_stock    ON broker_daily(stock_code, trade_date);
CREATE INDEX IF NOT EXISTS idx_broker_daily_net      ON broker_daily(net_shares DESC);

-- ── 八大公股日彙總（全市場，單次 API call）──────────────────────────────────────
CREATE TABLE IF NOT EXISTS gov_bank_daily (
    id          BIGSERIAL PRIMARY KEY,
    stock_code  VARCHAR(10)  NOT NULL,
    trade_date  DATE         NOT NULL,
    bank_name   VARCHAR(20)  NOT NULL,
    buy_shares  BIGINT       DEFAULT 0,
    sell_shares BIGINT       DEFAULT 0,
    net_shares  BIGINT       GENERATED ALWAYS AS (buy_shares - sell_shares) STORED,
    buy_amount  NUMERIC(20,2),
    sell_amount NUMERIC(20,2),
    created_at  TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE(stock_code, trade_date, bank_name)
);

CREATE INDEX IF NOT EXISTS idx_gov_bank_date  ON gov_bank_daily(trade_date);
CREATE INDEX IF NOT EXISTS idx_gov_bank_stock ON gov_bank_daily(stock_code, trade_date);

-- ── 暴跌日承接事件記錄（分析結果，長期保留）─────────────────────────────────────
CREATE TABLE IF NOT EXISTS crash_absorption_events (
    id               BIGSERIAL PRIMARY KEY,
    stock_code       VARCHAR(10) NOT NULL,
    crash_date       DATE        NOT NULL,
    drop_pct         NUMERIC(6,2),           -- 當日跌幅 %
    volume_ratio     NUMERIC(6,2),           -- 量能比（vs 20日均量）
    -- 三大法人
    foreign_net      BIGINT,
    trust_net        BIGINT,
    dealer_net       BIGINT,
    total_inst_net   BIGINT,
    -- 外資主力分點
    top_foreign_broker VARCHAR(50),
    top_foreign_net    BIGINT,
    -- 八大公股合計
    gov_bank_net     BIGINT,
    gov_bank_detail  JSONB,                  -- {兆豐: +100, 合庫: -200, ...}
    -- 融資
    margin_change    BIGINT,                 -- 融資增減（張）
    -- 評分
    absorption_score NUMERIC(5,1),           -- 0-10，越高越強力承接
    signal_tag       VARCHAR(20),            -- STRONG_BUY / WATCH / AVOID
    note             TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(stock_code, crash_date)
);

CREATE INDEX IF NOT EXISTS idx_crash_date  ON crash_absorption_events(crash_date);
CREATE INDEX IF NOT EXISTS idx_crash_score ON crash_absorption_events(absorption_score DESC);

-- ── API 使用量追蹤（防崩潰核心）────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS api_usage_log (
    id          BIGSERIAL   PRIMARY KEY,
    log_date    DATE        NOT NULL DEFAULT CURRENT_DATE,
    api_name    VARCHAR(30) NOT NULL DEFAULT 'finmind',
    call_count  INT         NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(log_date, api_name)
);

-- ── institutional_daily（保留 momentum-screener 的表，補充欄位）────────────────
CREATE TABLE IF NOT EXISTS institutional_daily (
    id             BIGSERIAL PRIMARY KEY,
    stock_code     VARCHAR(10) NOT NULL,
    trade_date     DATE        NOT NULL,
    foreign_buy    BIGINT      DEFAULT 0,
    foreign_sell   BIGINT      DEFAULT 0,
    foreign_net    BIGINT      DEFAULT 0,
    trust_net      BIGINT      DEFAULT 0,
    dealer_net     BIGINT      DEFAULT 0,
    total_net      BIGINT      DEFAULT 0,
    foreign_consec INT         DEFAULT 0,   -- 外資連買(+)/連賣(-) 天數
    foreign_ratio  NUMERIC(6,2),            -- 外資持股比例 %
    margin_shares  BIGINT      DEFAULT 0,   -- 融資餘額（張）
    margin_change  BIGINT      DEFAULT 0,   -- 融資增減（張）
    updated_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(stock_code, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_inst_daily_date  ON institutional_daily(trade_date);
CREATE INDEX IF NOT EXISTS idx_inst_daily_stock ON institutional_daily(stock_code, trade_date);

-- ── 自動清理策略（防止磁碟撐爆）──────────────────────────────────────────────
-- broker_daily: 保留最近 30 天（暴跌日分點資料量大）
-- gov_bank_daily: 保留最近 90 天
-- api_usage_log: 保留最近 30 天

-- 清理函式（由 stock-ai-agent 每日凌晨 01:00 執行）
CREATE OR REPLACE FUNCTION cleanup_old_data() RETURNS void AS $$
BEGIN
    DELETE FROM broker_daily       WHERE created_at < NOW() - INTERVAL '30 days';
    DELETE FROM gov_bank_daily     WHERE created_at < NOW() - INTERVAL '90 days';
    DELETE FROM api_usage_log      WHERE log_date   < CURRENT_DATE - 30;
    DELETE FROM crash_absorption_events WHERE created_at < NOW() - INTERVAL '365 days';
END;
$$ LANGUAGE plpgsql;

-- ── API 使用量計數器函式 ────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION increment_api_usage(p_api VARCHAR DEFAULT 'finmind', p_n INT DEFAULT 1)
RETURNS INT AS $$
DECLARE v_count INT;
BEGIN
    INSERT INTO api_usage_log(log_date, api_name, call_count)
    VALUES (CURRENT_DATE, p_api, p_n)
    ON CONFLICT (log_date, api_name) DO UPDATE
        SET call_count = api_usage_log.call_count + p_n,
            updated_at = NOW()
    RETURNING call_count INTO v_count;
    RETURN v_count;
END;
$$ LANGUAGE plpgsql;

-- 初始化今日計數器
INSERT INTO api_usage_log(log_date, api_name, call_count)
VALUES (CURRENT_DATE, 'finmind', 0), (CURRENT_DATE, 'tej', 0)
ON CONFLICT DO NOTHING;
