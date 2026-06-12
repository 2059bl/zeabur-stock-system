-- ============================================================
-- 台股量化交易系統 PostgreSQL 初始化腳本 v2.5
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

CREATE TYPE news_test_status AS ENUM ('NORMAL', 'BULL_TRAP', 'BEAR_CLIMB', 'PENDING');
CREATE TYPE institution_flow_type AS ENUM ('DOUBLE_SELL', 'SINGLE_SELL', 'HOLD_OR_BUY', 'DOUBLE_BUY');
CREATE TYPE signal_level_type AS ENUM ('NORMAL', 'WATCH', 'WARNING', 'DANGER', 'EXTREME');
CREATE TYPE bear_stage_type AS ENUM ('T1_EARLY', 'T2_MID', 'T3_CONFIRM', 'REVERSAL');

CREATE TABLE IF NOT EXISTS stocks (
    stock_code        VARCHAR(10) PRIMARY KEY,
    stock_name        VARCHAR(50) NOT NULL,
    market            VARCHAR(10) NOT NULL DEFAULT 'TWSE',
    sector            VARCHAR(50),
    industry          VARCHAR(50),
    is_active         BOOLEAN DEFAULT TRUE,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO stocks (stock_code, stock_name, market, sector, industry) VALUES
    ('6442', '光聖',        'OTC',  '光通訊',   '光纖元件'),
    ('4979', '華星光',      'OTC',  '光通訊',   '光纖元件'),
    ('2330', '台積電',      'TWSE', '半導體',   'IC製造'),
    ('2317', '鴻海',        'TWSE', 'AI伺服器', '其他電子'),
    ('2412', '中華電',      'TWSE', '電信內需', '通信網路'),
    ('0050', '元大台灣50',  'TWSE', 'ETF',      'ETF')
ON CONFLICT (stock_code) DO NOTHING;

CREATE TABLE IF NOT EXISTS stock_prices (
    id          BIGSERIAL PRIMARY KEY,
    stock_code  VARCHAR(10) NOT NULL REFERENCES stocks(stock_code),
    trade_date  DATE NOT NULL,
    open_price  NUMERIC(10, 2),
    high_price  NUMERIC(10, 2),
    low_price   NUMERIC(10, 2),
    close_price NUMERIC(10, 2) NOT NULL,
    volume      BIGINT,
    change_pct  NUMERIC(6, 2),
    UNIQUE (stock_code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_prices_code_date ON stock_prices (stock_code, trade_date DESC);

CREATE TABLE IF NOT EXISTS stock_indicators (
    id              BIGSERIAL PRIMARY KEY,
    stock_code      VARCHAR(10) NOT NULL REFERENCES stocks(stock_code),
    trade_date      DATE NOT NULL,
    sma_5           NUMERIC(10, 2),
    sma_20          NUMERIC(10, 2),
    sma_60          NUMERIC(10, 2),
    sma_120         NUMERIC(10, 2),
    rsi_14          NUMERIC(6, 2),
    macd            NUMERIC(10, 4),
    macd_signal     NUMERIC(10, 4),
    macd_histogram  NUMERIC(10, 4),
    bias_rate       NUMERIC(6, 2),
    -- 籌碼欄位（由 FinMind agent 填入）
    margin_balance            BIGINT,
    margin_short_shares       BIGINT,
    short_to_margin_ratio     NUMERIC(6, 2),
    foreign_net_buy           BIGINT,
    investment_trust_net_buy  BIGINT,
    dealer_net_buy            BIGINT,
    bad_news_test_status      news_test_status DEFAULT 'NORMAL',
    short_trend_confirmed     BOOLEAN DEFAULT FALSE,
    institution_flow          institution_flow_type DEFAULT 'HOLD_OR_BUY',
    bear_signal_score         NUMERIC(5, 2),
    bear_signal_level         signal_level_type DEFAULT 'NORMAL',
    updated_at                TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (stock_code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_ind_latest ON stock_indicators (stock_code, trade_date DESC);

CREATE TABLE IF NOT EXISTS market_indicators (
    id                    BIGSERIAL PRIMARY KEY,
    trade_date            DATE NOT NULL UNIQUE,
    taiex_close           NUMERIC(10, 2),
    foreign_net_total     BIGINT,
    foreign_net_cum_20d   BIGINT,
    foreign_sell_days     INT,
    futures_net_short     BIGINT,
    futures_short_5d_chg  NUMERIC(8, 2),
    usdtwd_rate           NUMERIC(7, 4),
    trust_sell_days       INT,
    trust_net_total       BIGINT,
    market_signal_score   NUMERIC(5, 2),
    market_signal_level   signal_level_type DEFAULT 'NORMAL',
    institution_flow      institution_flow_type DEFAULT 'HOLD_OR_BUY',
    created_at            TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ai_reports (
    id              BIGSERIAL PRIMARY KEY,
    stock_code      VARCHAR(10) REFERENCES stocks(stock_code),
    report_date     DATE NOT NULL,
    report_type     VARCHAR(20) NOT NULL,
    agent_model     VARCHAR(50),
    technical_summary   TEXT,
    cot_reasoning       TEXT,
    final_score         NUMERIC(5, 2),
    recommendation      VARCHAR(20),
    risk_level          VARCHAR(10),
    confidence          NUMERIC(4, 2),
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (stock_code, report_date, report_type)
);

CREATE TABLE IF NOT EXISTS workflow_logs (
    id              BIGSERIAL PRIMARY KEY,
    workflow_name   VARCHAR(100) NOT NULL,
    status          VARCHAR(20) NOT NULL,
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    duration_ms     INT,
    error_message   TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id              BIGSERIAL PRIMARY KEY,
    sent_at         TIMESTAMPTZ DEFAULT NOW(),
    message_text    TEXT,
    is_sent         BOOLEAN DEFAULT FALSE,
    sent_at_actual  TIMESTAMPTZ
);

CREATE OR REPLACE VIEW latest_indicators AS
SELECT DISTINCT ON (i.stock_code)
    i.*, s.stock_name, s.sector, s.industry,
    CASE WHEN i.sma_5 < i.sma_20 AND i.sma_20 < i.sma_60 THEN TRUE ELSE FALSE END AS is_bear_alignment,
    CASE
        WHEN i.foreign_net_buy < 0 AND i.investment_trust_net_buy < 0 THEN 'DOUBLE_SELL'::institution_flow_type
        WHEN i.foreign_net_buy < 0 OR  i.investment_trust_net_buy < 0 THEN 'SINGLE_SELL'::institution_flow_type
        ELSE 'HOLD_OR_BUY'::institution_flow_type
    END AS institution_flow_signal
FROM stock_indicators i
JOIN stocks s ON s.stock_code = i.stock_code
ORDER BY i.stock_code, i.trade_date DESC;

CREATE OR REPLACE VIEW bear_strategy_candidates AS
SELECT
    li.*, ar.final_score AS ai_score, ar.recommendation AS ai_recommendation,
    ar.cot_reasoning AS ai_reasoning, mi.market_signal_score, mi.market_signal_level
FROM latest_indicators li
LEFT JOIN LATERAL (
    SELECT * FROM ai_reports
    WHERE stock_code = li.stock_code AND report_type = 'DECISION'
    ORDER BY created_at DESC LIMIT 1
) ar ON TRUE
LEFT JOIN market_indicators mi ON mi.trade_date = li.trade_date
WHERE li.is_bear_alignment = TRUE
  AND li.institution_flow_signal IN ('DOUBLE_SELL','SINGLE_SELL')
ORDER BY li.bear_signal_score DESC NULLS LAST;
