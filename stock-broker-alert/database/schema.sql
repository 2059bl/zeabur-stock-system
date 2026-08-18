-- ============================================================
-- stock-broker-alert Migration v5
-- 基於 migration_v4.sql，新增 broker 追蹤系統所需表
-- 執行前確認 migration_v4.sql 已完成
-- ============================================================

-- ── 成長股 Universe ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS growth_stock_universe (
    id          SERIAL PRIMARY KEY,
    stock_code  VARCHAR(10)  NOT NULL UNIQUE,
    stock_name  VARCHAR(50),
    sector      VARCHAR(30),
    active      BOOLEAN      DEFAULT TRUE,
    added_at    TIMESTAMPTZ  DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS etf_universe (
    id          SERIAL PRIMARY KEY,
    etf_code    VARCHAR(10)  NOT NULL UNIQUE,
    etf_name    VARCHAR(50),
    etf_type    VARCHAR(20),
    active      BOOLEAN      DEFAULT TRUE,
    added_at    TIMESTAMPTZ  DEFAULT NOW()
);

-- ── 個股日行情（Cache）──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stock_daily (
    id          BIGSERIAL    PRIMARY KEY,
    trade_date  DATE         NOT NULL,
    stock_code  VARCHAR(10)  NOT NULL,
    open_price  NUMERIC(10,2),
    high_price  NUMERIC(10,2),
    low_price   NUMERIC(10,2),
    close_price NUMERIC(10,2),
    change_pct  NUMERIC(6,2),
    volume      BIGINT,
    avg_20d_vol BIGINT,                    -- 20日均量（用於 volume_ratio）
    volume_ratio NUMERIC(6,2),             -- 今日量 / 20日均量
    created_at  TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE(trade_date, stock_code)
);

CREATE INDEX IF NOT EXISTS idx_stock_daily_date  ON stock_daily(trade_date);
CREATE INDEX IF NOT EXISTS idx_stock_daily_stock ON stock_daily(stock_code, trade_date);

-- ── 融資融券日資料 ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS margin_daily (
    id               BIGSERIAL   PRIMARY KEY,
    trade_date       DATE        NOT NULL,
    stock_code       VARCHAR(10) NOT NULL,
    margin_balance   BIGINT      DEFAULT 0,    -- 融資餘額（張）
    margin_buy       BIGINT      DEFAULT 0,    -- 融資買進
    margin_sell      BIGINT      DEFAULT 0,    -- 融資賣出
    margin_change    BIGINT      DEFAULT 0,    -- 融資增減（當日）
    short_balance    BIGINT      DEFAULT 0,    -- 融券餘額
    short_change     BIGINT      DEFAULT 0,    -- 融券增減
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(trade_date, stock_code)
);

CREATE INDEX IF NOT EXISTS idx_margin_daily_date  ON margin_daily(trade_date);
CREATE INDEX IF NOT EXISTS idx_margin_daily_stock ON margin_daily(stock_code, trade_date);

-- ── Broker Watchlist（關鍵追蹤分點）──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS broker_watchlist (
    id                  SERIAL       PRIMARY KEY,
    broker_code         VARCHAR(10)  NOT NULL UNIQUE,
    broker_name         VARCHAR(50)  NOT NULL,
    first_detected_date DATE,
    detected_stocks     TEXT[],                   -- 曾承接的股票代碼陣列
    detected_sectors    TEXT[],                   -- 涉及族群
    total_net_buy       BIGINT       DEFAULT 0,   -- 累計淨買超（張）
    max_absorption_ratio NUMERIC(6,2) DEFAULT 0,  -- 歷史最高吞噬率 %
    blood_selling_dates DATE[],                   -- 暴跌日承接日期
    blood_selling_count INT          DEFAULT 0,
    broker_score        INT          DEFAULT 0,
    day_trade_score     NUMERIC(5,2) DEFAULT 0,   -- 0-100，越高越像隔日沖
    day_trade_risk      VARCHAR(10)  DEFAULT 'LOW', -- LOW / MEDIUM / HIGH
    confidence_score    NUMERIC(5,2) DEFAULT 50,  -- 0-100
    active              BOOLEAN      DEFAULT TRUE,
    notes               TEXT,
    created_at          TIMESTAMPTZ  DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_watchlist_score  ON broker_watchlist(broker_score DESC);
CREATE INDEX IF NOT EXISTS idx_watchlist_active ON broker_watchlist(active);

-- ── 分點每日動作記錄（追蹤用，保留90天）──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS broker_daily_actions (
    id              BIGSERIAL    PRIMARY KEY,
    trade_date      DATE         NOT NULL,
    stock_code      VARCHAR(10)  NOT NULL,
    broker_code     VARCHAR(10)  NOT NULL,
    broker_name     VARCHAR(50),
    buy_volume      BIGINT       DEFAULT 0,
    sell_volume     BIGINT       DEFAULT 0,
    net_volume      BIGINT       DEFAULT 0,         -- GENERATED ALWAYS 在應用層計算
    net_amount      NUMERIC(20,2) DEFAULT 0,        -- 估算金額（元）
    stock_volume    BIGINT       DEFAULT 0,         -- 當日成交量
    absorption_ratio NUMERIC(6,2),                  -- net_volume / stock_volume * 100
    is_blood_day    BOOLEAN      DEFAULT FALSE,      -- 是否為暴跌日
    day_change_pct  NUMERIC(6,2),                   -- 當日跌幅
    in_watchlist    BOOLEAN      DEFAULT FALSE,
    created_at      TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE(trade_date, stock_code, broker_code)
);

CREATE INDEX IF NOT EXISTS idx_bda_date    ON broker_daily_actions(trade_date);
CREATE INDEX IF NOT EXISTS idx_bda_broker  ON broker_daily_actions(broker_code, trade_date);
CREATE INDEX IF NOT EXISTS idx_bda_blood   ON broker_daily_actions(is_blood_day, trade_date);

-- ── 已觸發警報記錄 ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS broker_alerts (
    id              BIGSERIAL    PRIMARY KEY,
    alert_date      DATE         NOT NULL DEFAULT CURRENT_DATE,
    broker_code     VARCHAR(10)  NOT NULL,
    broker_name     VARCHAR(50),
    stock_code      VARCHAR(10)  NOT NULL,
    stock_name      VARCHAR(50),
    alert_type      VARCHAR(30)  NOT NULL,   -- ABNORMAL_BUY / CONSECUTIVE / DAY_TRADE_RISK / BLOOD_ABSORPTION
    alert_level     VARCHAR(10)  DEFAULT 'WARN',  -- INFO / WARN / CRITICAL
    net_volume      BIGINT,
    absorption_ratio NUMERIC(6,2),
    net_amount      NUMERIC(20,2),
    consecutive_days INT,
    broker_score    INT,
    message         TEXT,
    telegram_sent   BOOLEAN      DEFAULT FALSE,
    telegram_sent_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE(alert_date, broker_code, stock_code, alert_type)
);

CREATE INDEX IF NOT EXISTS idx_alerts_date    ON broker_alerts(alert_date DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_broker  ON broker_alerts(broker_code);
CREATE INDEX IF NOT EXISTS idx_alerts_unsent  ON broker_alerts(telegram_sent, created_at);

-- ── 暴跌日分析完整記錄（擴充版，相容 crash_absorption_events）──────────────────
CREATE TABLE IF NOT EXISTS blood_absorption_report (
    id                  BIGSERIAL    PRIMARY KEY,
    trade_date          DATE         NOT NULL,
    stock_code          VARCHAR(10)  NOT NULL,
    stock_name          VARCHAR(50),
    sector              VARCHAR(30),
    change_pct          NUMERIC(6,2),
    volume              BIGINT,
    volume_ratio        NUMERIC(6,2),
    -- 融資
    margin_change       BIGINT,
    financing_absorbed  VARCHAR(30)  DEFAULT 'FINANCING_DATA_UNAVAILABLE',
    -- 三大法人
    foreign_net         BIGINT       DEFAULT 0,
    trust_net           BIGINT       DEFAULT 0,
    dealer_net          BIGINT       DEFAULT 0,
    total_inst_net      BIGINT       DEFAULT 0,
    -- 公股
    gov_bank_net        BIGINT       DEFAULT 0,
    gov_bank_detail     JSONB,
    pub_bank_status     VARCHAR(30)  DEFAULT 'PUBLIC_BANK_DATA_UNAVAILABLE',
    -- 主要承接分點（Top 3）
    top_broker_1_code   VARCHAR(10),
    top_broker_1_name   VARCHAR(50),
    top_broker_1_net    BIGINT,
    top_broker_2_code   VARCHAR(10),
    top_broker_2_name   VARCHAR(50),
    top_broker_2_net    BIGINT,
    top_broker_3_code   VARCHAR(10),
    top_broker_3_name   VARCHAR(50),
    top_broker_3_net    BIGINT,
    -- 評分
    absorption_ratio    NUMERIC(6,2),
    absorption_score    NUMERIC(5,1),
    is_blood_absorption BOOLEAN      DEFAULT FALSE,
    principal_type      VARCHAR(30),   -- 公股承接/外資承接/主力地緣型/特定券商/高頻隔日沖/混合型/無法判定
    signal_tag          VARCHAR(20),   -- STRONG_BUY / WATCH / AVOID
    -- 資料品質
    data_complete       BOOLEAN      DEFAULT FALSE,
    data_issues         TEXT[],
    created_at          TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE(trade_date, stock_code)
);

CREATE INDEX IF NOT EXISTS idx_bar_date      ON blood_absorption_report(trade_date);
CREATE INDEX IF NOT EXISTS idx_bar_blood     ON blood_absorption_report(is_blood_absorption, trade_date);
CREATE INDEX IF NOT EXISTS idx_bar_score     ON blood_absorption_report(absorption_score DESC);

-- ── API 呼叫詳細日誌（擴充版）────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS api_request_log (
    id                  BIGSERIAL    PRIMARY KEY,
    log_datetime        TIMESTAMPTZ  DEFAULT NOW(),
    provider            VARCHAR(20)  NOT NULL,   -- finmind / tej
    endpoint            VARCHAR(100),
    dataset             VARCHAR(50),
    query_date          DATE,
    stock_code          VARCHAR(10),
    http_status         INT,
    api_status          INT,
    row_count           INT          DEFAULT 0,
    is_complete         BOOLEAN      DEFAULT TRUE,
    rate_limited        BOOLEAN      DEFAULT FALSE,
    api_error           BOOLEAN      DEFAULT FALSE,
    error_message       TEXT,
    response_ms         INT
);

CREATE INDEX IF NOT EXISTS idx_arl_datetime ON api_request_log(log_datetime DESC);
CREATE INDEX IF NOT EXISTS idx_arl_provider ON api_request_log(provider, log_datetime);

-- ── 隔日沖行為追蹤 ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS day_trade_history (
    id              BIGSERIAL    PRIMARY KEY,
    broker_code     VARCHAR(10)  NOT NULL,
    stock_code      VARCHAR(10)  NOT NULL,
    buy_date        DATE         NOT NULL,
    sell_date       DATE,
    buy_volume      BIGINT,
    sell_volume     BIGINT,
    is_day_trade    BOOLEAN      DEFAULT FALSE,
    created_at      TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE(broker_code, stock_code, buy_date)
);

CREATE INDEX IF NOT EXISTS idx_dth_broker ON day_trade_history(broker_code, buy_date);

-- ── 便利視圖：今日 API 用量 ──────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_api_quota_today AS
SELECT
    provider,
    SUM(1) AS request_count,
    SUM(row_count) AS total_rows,
    SUM(CASE WHEN rate_limited THEN 1 ELSE 0 END) AS rate_limit_hits,
    SUM(CASE WHEN api_error THEN 1 ELSE 0 END) AS error_count,
    MAX(log_datetime) AS last_request
FROM api_request_log
WHERE log_datetime::date = CURRENT_DATE
GROUP BY provider;

-- ── 便利視圖：Broker Watchlist 摘要 ──────────────────────────────────────────────
CREATE OR REPLACE VIEW v_broker_watchlist_summary AS
SELECT
    bw.broker_code,
    bw.broker_name,
    bw.broker_score,
    bw.day_trade_risk,
    bw.blood_selling_count,
    bw.total_net_buy,
    bw.max_absorption_ratio,
    bw.detected_sectors,
    COUNT(ba.id) AS total_alerts,
    MAX(ba.alert_date) AS last_alert_date
FROM broker_watchlist bw
LEFT JOIN broker_alerts ba ON bw.broker_code = ba.broker_code
WHERE bw.active = TRUE
GROUP BY bw.broker_code, bw.broker_name, bw.broker_score,
         bw.day_trade_risk, bw.blood_selling_count, bw.total_net_buy,
         bw.max_absorption_ratio, bw.detected_sectors
ORDER BY bw.broker_score DESC;

-- ── 清理函式更新（包含新表）──────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION cleanup_old_data_v5() RETURNS void AS $$
BEGIN
    DELETE FROM broker_daily             WHERE created_at < NOW() - INTERVAL '30 days';
    DELETE FROM gov_bank_daily           WHERE created_at < NOW() - INTERVAL '90 days';
    DELETE FROM api_usage_log            WHERE log_date   < CURRENT_DATE - 30;
    DELETE FROM api_request_log          WHERE log_datetime < NOW() - INTERVAL '30 days';
    DELETE FROM broker_daily_actions     WHERE created_at < NOW() - INTERVAL '90 days';
    DELETE FROM broker_alerts            WHERE created_at < NOW() - INTERVAL '365 days';
    DELETE FROM day_trade_history        WHERE created_at < NOW() - INTERVAL '60 days';
    DELETE FROM stock_daily              WHERE created_at < NOW() - INTERVAL '60 days';
    DELETE FROM margin_daily             WHERE created_at < NOW() - INTERVAL '60 days';
END;
$$ LANGUAGE plpgsql;
