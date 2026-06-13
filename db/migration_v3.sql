-- ============================================================
-- Migration v3：外資持股比例、籌碼強化、新聞情緒、回測框架
-- 安全：重複執行不會出錯
-- ============================================================

ALTER TABLE stock_indicators
    ADD COLUMN IF NOT EXISTS foreign_holding_ratio    NUMERIC(6,2),   -- 外資持股比例 %
    ADD COLUMN IF NOT EXISTS foreign_consecutive_days INT DEFAULT 0,  -- 外資連買(+)/連賣(-)天數
    ADD COLUMN IF NOT EXISTS short_cover_days         NUMERIC(8,2),   -- 融券回補預估天數
    ADD COLUMN IF NOT EXISTS margin_trend_5d          NUMERIC(6,2),   -- 5日融資餘額變化%
    ADD COLUMN IF NOT EXISTS composite_score          NUMERIC(5,2),   -- 綜合空頭評分 0~100
    ADD COLUMN IF NOT EXISTS sentiment_score          NUMERIC(5,2);   -- 新聞情緒 -1.0~+1.0

CREATE TABLE IF NOT EXISTS news_cache (
    id          BIGSERIAL PRIMARY KEY,
    stock_code  VARCHAR(10) REFERENCES stocks(stock_code),
    news_date   DATE NOT NULL,
    title       TEXT NOT NULL,
    source      VARCHAR(100),
    sentiment   NUMERIC(4,3),
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(stock_code, news_date, title)
);
CREATE INDEX IF NOT EXISTS idx_news_code_date ON news_cache(stock_code, news_date DESC);

CREATE TABLE IF NOT EXISTS backtest_results (
    id              BIGSERIAL PRIMARY KEY,
    strategy_name   VARCHAR(100) NOT NULL,
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,
    total_signals   INT,
    win_signals     INT,
    win_rate        NUMERIC(5,2),
    avg_return_5d   NUMERIC(6,2),
    avg_return_10d  NUMERIC(6,2),
    avg_return_20d  NUMERIC(6,2),
    max_drawdown    NUMERIC(6,2),
    sharpe_ratio    NUMERIC(6,3),
    details         JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 更新 latest_indicators view 加入新欄位
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

-- 更新 bear_strategy_candidates view 加入 composite_score 排序
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
ORDER BY li.composite_score DESC NULLS LAST, li.bear_signal_score DESC NULLS LAST;
