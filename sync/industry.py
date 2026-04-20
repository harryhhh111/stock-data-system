"""sync/industry.py — 行业分类同步（A 股 / 港股 / 美股）。"""

from __future__ import annotations

import time

from ._utils import logger, execute
from db import batch_update_industry


def sync_industry() -> dict:
    """同步申万一级行业分类数据到 stock_info.industry。

    流程：
      1. 通过 akshare 拉取 31 个申万一级行业的成分股
      2. UPDATE stock_info SET industry = ? WHERE stock_code = ?

    Returns:
        {"total": int, "updated": int, "not_in_stock_info": int, "industry_count": int, "elapsed": float}
    """
    from fetchers.industry import fetch_sw_industry, get_industry_distribution

    logger.info("开始同步行业分类数据...")

    # 拉取数据
    results = fetch_sw_industry()
    if not results:
        logger.warning("行业分类数据为空")
        return {"total": 0, "updated": 0, "industry_count": 0}

    # 批量 UPDATE stock_info.industry
    updated = 0
    not_found = 0
    t0 = time.time()

    # 先获取 stock_info 中已有的 A 股代码集合
    existing_rows = execute(
        "SELECT stock_code FROM stock_info WHERE market = 'CN_A'",
        fetch=True,
    )
    existing_codes = {r[0] for r in existing_rows}
    logger.info("stock_info 中 A 股: %d 只", len(existing_codes))

    # 构建 code → industry 映射
    code_industry = {}
    for item in results:
        code = item["stock_code"]
        if code in existing_codes:
            code_industry[code] = item["industry_name"]
        else:
            not_found += 1

    logger.info(
        "行业数据: %d 只, 在 stock_info 中: %d 只, 未找到: %d 只",
        len(results),
        len(code_industry),
        not_found,
    )

    # 批量 UPDATE（每 500 只一批）
    batch_size = 500
    codes_list = list(code_industry.keys())

    for i in range(0, len(codes_list), batch_size):
        batch = codes_list[i : i + batch_size]
        # 使用 CASE WHEN 批量更新
        case_parts = []
        codes_str = []
        for code in batch:
            industry = code_industry[code].replace("'", "''")
            case_parts.append(f"WHEN '{code}' THEN '{industry}'")
            codes_str.append(f"'{code}'")

        sql = f"""
            UPDATE stock_info
            SET industry = CASE stock_code
                {" ".join(case_parts)}
            END,
            updated_at = NOW()
            WHERE stock_code IN ({", ".join(codes_str)})
            AND market = 'CN_A'
        """
        execute(sql, commit=True)
        updated += len(batch)

        if (i + batch_size) % 2000 == 0 or i + batch_size >= len(codes_list):
            logger.info(
                "行业写入进度: %d/%d (%.0f%%)",
                min(i + batch_size, len(codes_list)),
                len(codes_list),
                min(i + batch_size, len(codes_list)) / len(codes_list) * 100,
            )

    elapsed = time.time() - t0

    # 统计行业分布
    dist_df = get_industry_distribution(results)

    result = {
        "total": len(results),
        "updated": updated,
        "not_in_stock_info": not_found,
        "industry_count": len(dist_df),
        "elapsed": elapsed,
    }

    logger.info(
        "行业分类同步完成: 总计=%d, 更新=%d, 不在stock_info=%d, 行业数=%d, 耗时=%.1fs",
        len(results),
        updated,
        not_found,
        len(dist_df),
        elapsed,
    )

    # 打印行业分布
    logger.info("行业分布:")
    for _, row in dist_df.iterrows():
        logger.info("  %s: %d 只", row["industry_name"], row["stock_count"])

    return result


def sync_us_industry() -> dict:
    """同步美股行业分类数据（SEC EDGAR SIC Code）到 stock_info.industry。

    Returns:
        {"total": int, "updated": int, "empty_industry": int, "industry_count": int, "elapsed": float}
    """
    from fetchers.industry import fetch_us_industry, get_industry_distribution

    logger.info("开始同步美股行业分类数据...")

    # Step 1: 查询有 CIK 的美股
    rows = execute(
        "SELECT stock_code, cik FROM stock_info WHERE market = 'US' AND cik IS NOT NULL",
        fetch=True,
    )
    stocks = [{"stock_code": r[0], "cik": r[1]} for r in rows]
    logger.info("stock_info 中有 CIK 的美股: %d 只", len(stocks))

    if not stocks:
        logger.warning("没有找到有 CIK 的美股")
        return {"total": 0, "updated": 0, "empty_industry": 0, "industry_count": 0}

    # Step 2: 拉取行业数据
    results = fetch_us_industry(stocks)
    if not results:
        logger.warning("美股行业分类数据为空")
        return {
            "total": len(stocks),
            "updated": 0,
            "empty_industry": 0,
            "industry_count": 0,
        }

    # Step 3: 批量 UPDATE stock_info.industry
    updated = 0
    empty_industry = 0
    t0 = time.time()

    # 构建 code → industry 映射
    code_industry = {}
    for item in results:
        code_industry[item["stock_code"]] = item["industry_name"]
        if not item["industry_name"]:
            empty_industry += 1

    # 批量 UPDATE（每 500 只一批）
    batch_size = 500
    codes_list = list(code_industry.keys())

    for i in range(0, len(codes_list), batch_size):
        batch = codes_list[i : i + batch_size]
        # 使用 CASE WHEN 批量更新
        case_parts = []
        codes_str = []
        for code in batch:
            industry = code_industry[code].replace("'", "''")
            case_parts.append(f"WHEN '{code}' THEN '{industry}'")
            codes_str.append(f"'{code}'")

        sql = f"""
            UPDATE stock_info
            SET industry = CASE stock_code
                {" ".join(case_parts)}
            END,
            updated_at = NOW()
            WHERE stock_code IN ({", ".join(codes_str)})
            AND market = 'US'
        """
        execute(sql, commit=True)
        updated += len(batch)

        if (i + batch_size) % 2000 == 0 or i + batch_size >= len(codes_list):
            logger.info(
                "美股行业写入进度: %d/%d (%.0f%%)",
                min(i + batch_size, len(codes_list)),
                len(codes_list),
                min(i + batch_size, len(codes_list)) / len(codes_list) * 100,
            )

    elapsed = time.time() - t0

    # 统计行业分布
    non_empty = [r for r in results if r["industry_name"]]
    dist_df = get_industry_distribution(non_empty)

    result = {
        "total": len(stocks),
        "updated": updated,
        "empty_industry": empty_industry,
        "industry_count": len(dist_df),
        "elapsed": elapsed,
    }

    logger.info(
        "美股行业分类同步完成: 总计=%d, 更新=%d, 空行业=%d, 行业数=%d, 耗时=%.1fs",
        len(stocks),
        updated,
        empty_industry,
        len(dist_df),
        elapsed,
    )

    # 打印行业分布（前 20 个）
    if not dist_df.empty:
        logger.info("行业分布 (前20):")
        for _, row in dist_df.head(20).iterrows():
            logger.info("  %s: %d 只", row["industry_name"], row["stock_count"])

    return result


def sync_hk_industry(force: bool = False) -> dict:
    """同步港股行业分类数据（东方财富 F10）到 stock_info.industry。

    边拉边写：每 50 只批量写入数据库，防止中断丢数据。
    断点续传：跳过 industry 已非空的记录（除非 force=True）。

    Returns:
        {"total": int, "updated": int, "empty_industry": int, "industry_count": int, "failed": int, "elapsed": float}
    """
    from fetchers.industry import fetch_hk_industry, get_industry_distribution

    logger.info("开始同步港股行业分类数据 (force=%s)...", force)

    if force:
        rows = execute(
            "SELECT stock_code FROM stock_info WHERE market = 'CN_HK'",
            fetch=True,
        )
    else:
        rows = execute(
            "SELECT stock_code FROM stock_info "
            "WHERE market = 'CN_HK' AND (industry IS NULL OR industry = '')",
            fetch=True,
        )

    stocks = [{"stock_code": r[0]} for r in rows]
    total = len(stocks)
    logger.info("待拉取港股行业: %d 只 (force=%s)", total, force)

    if not stocks:
        logger.info("所有港股已有行业数据，无需拉取")
        return {"total": 0, "updated": 0, "empty_industry": 0, "failed": 0}

    t0 = time.time()
    total_updated = 0
    empty_industry = 0
    all_results: list[dict[str, str]] = []

    def on_batch(batch_results: list[dict[str, str]]) -> None:
        nonlocal total_updated, empty_industry
        code_ind: dict[str, str] = {}
        for item in batch_results:
            ind = item.get("industry_name", "")
            if ind:
                code_ind[item["stock_code"]] = ind
            else:
                empty_industry += 1
        if code_ind:
            total_updated += batch_update_industry(code_ind, "CN_HK")

    results = fetch_hk_industry(stocks, on_batch=on_batch)
    if not results:
        logger.warning("港股行业分类数据为空")
        return {"total": total, "updated": 0, "empty_industry": 0, "failed": 0}

    total_failed = total - len(results)
    elapsed = time.time() - t0

    non_empty = [r for r in results if r.get("industry_name")]
    dist_df = get_industry_distribution(non_empty)

    result = {
        "total": total,
        "updated": total_updated,
        "empty_industry": empty_industry,
        "industry_count": len(dist_df),
        "failed": total_failed,
        "elapsed": elapsed,
    }

    logger.info(
        "港股行业分类同步完成: 总计=%d, 更新=%d, 空行业=%d, 失败=%d, 耗时=%.1fs",
        total,
        total_updated,
        empty_industry,
        total_failed,
        elapsed,
    )

    if not dist_df.empty:
        logger.info("行业分布 (前20):")
        for _, row in dist_df.head(20).iterrows():
            logger.info("  %s: %d 只", row["industry_name"], row["stock_count"])

    return result
