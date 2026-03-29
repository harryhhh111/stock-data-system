-- ============================================================
-- Migration: 给 stock_info.market 加 CHECK 约束
-- 执行：psql -U postgres -d stock_data -f scripts/add_market_check.sql
-- ============================================================

-- 1. 检查是否有脏数据（market 不在合法范围内）
DO $$
DECLARE
    dirty_count INTEGER;
    dirty_markets TEXT[];
BEGIN
    SELECT COUNT(*), ARRAY_AGG(DISTINCT market)
    INTO dirty_count, dirty_markets
    FROM stock_info
    WHERE market IS NULL OR market NOT IN ('CN_A', 'CN_HK', 'US');

    IF dirty_count > 0 THEN
        RAISE NOTICE '发现 % 条脏数据，market 值: %', dirty_count, dirty_markets;
        -- 将 'HK' 统一为 'CN_HK'
        UPDATE stock_info SET market = 'CN_HK' WHERE market = 'HK';
        RAISE NOTICE '已将 market=''HK'' 修正为 ''CN_HK''';
        -- 删除剩余脏数据（如果还有其他非法值）
        DELETE FROM stock_info WHERE market IS NULL OR market NOT IN ('CN_A', 'CN_HK', 'US');
        IF NOT FOUND THEN
            RAISE NOTICE '无其他脏数据需要删除';
        END IF;
    ELSE
        RAISE NOTICE '无脏数据，直接添加约束';
    END IF;
END $$;

-- 2. 添加 CHECK 约束
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_stock_info_market'
    ) THEN
        ALTER TABLE stock_info ADD CONSTRAINT chk_stock_info_market
            CHECK (market IN ('CN_A', 'CN_HK', 'US'));
        RAISE NOTICE 'CHECK 约束 chk_stock_info_market 已添加';
    ELSE
        RAISE NOTICE 'CHECK 约束 chk_stock_info_market 已存在，跳过';
    END IF;
END $$;

-- 3. 同样处理 sync_progress.market（虽然不是关键字段，保持一致）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_sync_progress_market'
    ) THEN
        -- 先清理脏数据
        DELETE FROM sync_progress WHERE market IS NULL OR market NOT IN ('CN_A', 'CN_HK', 'US');
        ALTER TABLE sync_progress ADD CONSTRAINT chk_sync_progress_market
            CHECK (market IN ('CN_A', 'CN_HK', 'US'));
        RAISE NOTICE 'CHECK 约束 chk_sync_progress_market 已添加';
    ELSE
        RAISE NOTICE 'CHECK 约束 chk_sync_progress_market 已存在，跳过';
    END IF;
END $$;

-- 4. 完成（init_pg.sql 已同步包含 CHECK 约束，无需额外操作）
