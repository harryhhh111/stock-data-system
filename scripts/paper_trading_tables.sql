-- ============================================================
-- Paper Trading Tables
-- PostgreSQL 16+
-- ============================================================

-- 模拟盘账户。account_id 由应用层生成 uuid hex，避免依赖数据库扩展。
CREATE TABLE IF NOT EXISTS paper_accounts (
    account_id      VARCHAR(32) PRIMARY KEY,
    account_name    VARCHAR(100) NOT NULL,
    strategy_name   VARCHAR(100) NOT NULL,
    preset_type     VARCHAR(20) NOT NULL DEFAULT 'normal',
    market          VARCHAR(10) NOT NULL,
    benchmark       VARCHAR(30),
    initial_capital DECIMAL(20,4) NOT NULL,
    cash            DECIMAL(20,4) NOT NULL,
    total_value     DECIMAL(20,4) NOT NULL,
    nav             DECIMAL(20,8) NOT NULL DEFAULT 1.0,
    fee_rate        DECIMAL(10,8) NOT NULL DEFAULT 0,
    slippage_bps    DECIMAL(10,4) NOT NULL DEFAULT 0,
    rebalance_rule  VARCHAR(30) NOT NULL DEFAULT 'strategy',
    config          JSONB NOT NULL DEFAULT '{}'::jsonb,
    status          VARCHAR(20) NOT NULL DEFAULT 'active',
    last_valued_at  TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_paper_account_market
        CHECK (market IN ('CN_A', 'CN_HK', 'US')),
    CONSTRAINT chk_paper_account_type
        CHECK (preset_type IN ('normal', 'composite')),
    CONSTRAINT chk_paper_account_status
        CHECK (status IN ('active', 'paused', 'archived')),
    CONSTRAINT chk_paper_account_capital
        CHECK (initial_capital > 0 AND cash >= 0 AND total_value >= 0 AND nav >= 0),
    CONSTRAINT chk_paper_account_costs
        CHECK (fee_rate >= 0 AND slippage_bps >= 0)
);

CREATE INDEX IF NOT EXISTS idx_paper_accounts_strategy ON paper_accounts(strategy_name);
CREATE INDEX IF NOT EXISTS idx_paper_accounts_market ON paper_accounts(market);
CREATE INDEX IF NOT EXISTS idx_paper_accounts_status ON paper_accounts(status);

-- 当前持仓快照。每个账户每只股票一行，非持仓股票不保留 0 仓位。
CREATE TABLE IF NOT EXISTS paper_positions (
    account_id      VARCHAR(32) NOT NULL,
    stock_code      VARCHAR(20) NOT NULL,
    market          VARCHAR(10) NOT NULL,
    sub_strategy    VARCHAR(50),
    shares          DECIMAL(20,6) NOT NULL,
    avg_cost        DECIMAL(20,6) NOT NULL,
    last_price      DECIMAL(20,6),
    market_value    DECIMAL(20,4) NOT NULL DEFAULT 0,
    weight          DECIMAL(12,8) NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_paper_positions PRIMARY KEY (account_id, stock_code),
    CONSTRAINT fk_paper_positions_account
        FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id) ON DELETE CASCADE,
    CONSTRAINT chk_paper_positions_market
        CHECK (market IN ('CN_A', 'CN_HK', 'US', 'CN_IDX')),
    CONSTRAINT chk_paper_positions_nonnegative
        CHECK (shares >= 0 AND avg_cost >= 0 AND market_value >= 0 AND weight >= 0)
);

CREATE INDEX IF NOT EXISTS idx_paper_positions_account ON paper_positions(account_id);
CREATE INDEX IF NOT EXISTS idx_paper_positions_stock ON paper_positions(stock_code, market);
CREATE INDEX IF NOT EXISTS idx_paper_positions_sub_strategy ON paper_positions(account_id, sub_strategy);

-- 模拟成交流水。调仓日生成，非调仓日不写成交。
CREATE TABLE IF NOT EXISTS paper_trades (
    trade_id        BIGSERIAL PRIMARY KEY,
    account_id      VARCHAR(32) NOT NULL,
    trade_date      DATE NOT NULL,
    stock_code      VARCHAR(20) NOT NULL,
    market          VARCHAR(10) NOT NULL,
    sub_strategy    VARCHAR(50),
    side            VARCHAR(10) NOT NULL,
    shares          DECIMAL(20,6) NOT NULL,
    price           DECIMAL(20,6) NOT NULL,
    amount          DECIMAL(20,4) NOT NULL,
    fee             DECIMAL(20,4) NOT NULL DEFAULT 0,
    slippage        DECIMAL(20,4) NOT NULL DEFAULT 0,
    reason          VARCHAR(100),
    signal_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_paper_trades_account
        FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id) ON DELETE CASCADE,
    CONSTRAINT chk_paper_trades_market
        CHECK (market IN ('CN_A', 'CN_HK', 'US', 'CN_IDX')),
    CONSTRAINT chk_paper_trades_side
        CHECK (side IN ('buy', 'sell')),
    CONSTRAINT chk_paper_trades_nonnegative
        CHECK (shares > 0 AND price >= 0 AND amount >= 0 AND fee >= 0 AND slippage >= 0)
);

CREATE INDEX IF NOT EXISTS idx_paper_trades_account_date ON paper_trades(account_id, trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_paper_trades_stock ON paper_trades(stock_code, market);
CREATE INDEX IF NOT EXISTS idx_paper_trades_sub_strategy ON paper_trades(account_id, sub_strategy);
-- 防止同一调仓日重复写入成交（执行引擎幂等保护）
CREATE UNIQUE INDEX IF NOT EXISTS uk_paper_trades_dedup
    ON paper_trades(account_id, trade_date, stock_code, side, COALESCE(sub_strategy, ''));

-- 每日净值快照。估值任务可按 (account_id, value_date) 幂等覆盖。
CREATE TABLE IF NOT EXISTS paper_nav_snapshots (
    account_id      VARCHAR(32) NOT NULL,
    value_date      DATE NOT NULL,
    cash            DECIMAL(20,4) NOT NULL,
    market_value    DECIMAL(20,4) NOT NULL,
    total_value     DECIMAL(20,4) NOT NULL,
    nav             DECIMAL(20,8) NOT NULL,
    benchmark_nav   DECIMAL(20,8),
    daily_return    DECIMAL(20,10),
    drawdown        DECIMAL(20,10),
    position_count  INTEGER NOT NULL DEFAULT 0,
    snapshot        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_paper_nav_snapshots PRIMARY KEY (account_id, value_date),
    CONSTRAINT fk_paper_nav_account
        FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id) ON DELETE CASCADE,
    CONSTRAINT chk_paper_nav_nonnegative
        CHECK (cash >= 0 AND market_value >= 0 AND total_value >= 0 AND nav >= 0 AND position_count >= 0)
);

CREATE INDEX IF NOT EXISTS idx_paper_nav_date ON paper_nav_snapshots(value_date DESC);

-- 策略运行记录。记录每日信号、目标权重、调仓建议和执行状态。
CREATE TABLE IF NOT EXISTS paper_strategy_runs (
    run_id          BIGSERIAL PRIMARY KEY,
    account_id      VARCHAR(32) NOT NULL,
    run_date        DATE NOT NULL,
    run_type        VARCHAR(20) NOT NULL DEFAULT 'valuation',
    status          VARCHAR(20) NOT NULL DEFAULT 'success',
    signals         JSONB NOT NULL DEFAULT '{}'::jsonb,
    allocation      JSONB NOT NULL DEFAULT '{}'::jsonb,
    target_positions JSONB NOT NULL DEFAULT '{}'::jsonb,
    trade_plan      JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message   TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,

    CONSTRAINT fk_paper_runs_account
        FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id) ON DELETE CASCADE,
    CONSTRAINT uk_paper_runs_account_date_type UNIQUE (account_id, run_date, run_type),
    CONSTRAINT chk_paper_runs_type
        CHECK (run_type IN ('valuation', 'rebalance')),
    CONSTRAINT chk_paper_runs_status
        CHECK (status IN ('success', 'failed', 'skipped'))
);

CREATE INDEX IF NOT EXISTS idx_paper_runs_account_date ON paper_strategy_runs(account_id, run_date DESC);
CREATE INDEX IF NOT EXISTS idx_paper_runs_status ON paper_strategy_runs(status);

-- ============================================================
-- 触发器：自动更新 updated_at
-- ============================================================

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_paper_accounts_updated_at
    BEFORE UPDATE ON paper_accounts
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER trg_paper_nav_snapshots_updated_at
    BEFORE UPDATE ON paper_nav_snapshots
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
