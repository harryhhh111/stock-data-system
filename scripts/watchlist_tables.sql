-- Personal shared watchlist. Metadata may refer to other market backends.
BEGIN;
CREATE TABLE IF NOT EXISTS watchlist_item (
    id BIGSERIAL PRIMARY KEY,
    market VARCHAR(10) NOT NULL CHECK (market IN ('US','CN_HK','CN_A')),
    stock_code VARCHAR(20) NOT NULL,
    stock_name VARCHAR(200) NOT NULL,
    group_name VARCHAR(80) NOT NULL DEFAULT '',
    note VARCHAR(2000) NOT NULL DEFAULT '',
    research_url VARCHAR(2000) NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (market, stock_code)
);
COMMIT;
