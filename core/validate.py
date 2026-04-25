#!/usr/bin/env python3
"""
validate.py — 财务数据校验引擎

检查已入库的 A 股 / 港股 / 美股财务数据质量：
  1. 异常值检测（极端比率、突变）
  2. 逻辑一致性（会计等式、子项 >= 母项）
  3. 跨源比对（当前仅单一源，记录为已知限制）
  4. 结果写入 validation_results 表 + 可选 JSON/CSV 输出

用法:
    python -m core.validate             # 校验全部市场
    python -m core.validate --market A  # 仅 A 股
    python -m core.validate --market HK # 仅港股
    python -m core.validate --market US # 仅美股
    python -m core.validate --market A --output json   # 额外输出 JSON
    python -m core.validate --market A --output csv    # 额外输出 CSV
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from decimal import Decimal
from typing import Optional, Any

import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("validate")


# ──────────────────────────────────────────────────────────
#  数据结构
# ──────────────────────────────────────────────────────────


@dataclass
class ValidationIssue:
    """一条校验异常。"""

    stock_code: str
    market: str
    report_date: str
    check_name: str
    severity: str  # "error" | "warning" | "info"
    field_name: str  # 触发的字段或关系
    actual_value: Optional[str] = None
    expected_value: Optional[str] = None
    message: str = ""
    suggestion: str = ""


@dataclass
class ValidationReport:
    """一次校验运行的汇总。"""

    started_at: str
    finished_at: str = ""
    market: str = ""
    total_rows_scanned: int = 0
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warning")

    @property
    def info_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "info")


# ──────────────────────────────────────────────────────────
#  建表（如果不存在）
# ──────────────────────────────────────────────────────────

_CREATE_VALIDATION_TABLE = """
CREATE TABLE IF NOT EXISTS validation_results (
    id              BIGSERIAL PRIMARY KEY,
    batch_id        VARCHAR(50) NOT NULL,       -- 批次标识，如 '2026-03-29_153300'
    stock_code      VARCHAR(20) NOT NULL,
    market          VARCHAR(10) NOT NULL,        -- 'CN_A' | 'CN_HK' | 'US'
    report_date     DATE NOT NULL,
    check_name      VARCHAR(80) NOT NULL,        -- 检查规则名称
    severity        VARCHAR(10) NOT NULL,        -- 'error' | 'warning' | 'info'
    field_name      VARCHAR(100),
    actual_value    TEXT,
    expected_value  TEXT,
    message         TEXT,
    suggestion      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_val_batch ON validation_results(batch_id);
CREATE INDEX IF NOT EXISTS idx_val_stock ON validation_results(stock_code);
CREATE INDEX IF NOT EXISTS idx_val_severity ON validation_results(severity);
CREATE INDEX IF NOT EXISTS idx_val_check ON validation_results(check_name);
"""


def ensure_table() -> None:
    """确保 validation_results 表存在。"""
    db.execute(_CREATE_VALIDATION_TABLE, commit=True)
    logger.info("validation_results 表就绪")


# ──────────────────────────────────────────────────────────
#  辅助函数
# ──────────────────────────────────────────────────────────


def _d(val: Any) -> Optional[float]:
    """把 Decimal / None / float 统一转为 float 或 None。"""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _market_filter(market: str, table_alias: str = "s") -> tuple[str, tuple]:
    """根据 market 参数生成 WHERE 子句。"""
    if market == "A":
        return f" AND {table_alias}.market = 'CN_A'", ()
    elif market == "HK":
        return f" AND {table_alias}.market = 'CN_HK'", ()
    elif market == "US":
        # US 数据在独立表中，无 stock_info 关联的市场字段
        return "", ()
    else:
        return "", ()


# ──────────────────────────────────────────────────────────
#  1. 异常值检测 — A 股 / 港股
# ──────────────────────────────────────────────────────────


def check_anomalies_cn_hk(market: str, issues: list[ValidationIssue]) -> int:
    """对 A 股/港股做异常值检测。返回扫描行数。"""
    market_clause, params = _market_filter(market, "s")

    sql = f"""
    SELECT
        i.stock_code, s.market, i.report_date, i.report_type,
        i.total_revenue, i.operating_revenue, i.net_profit, i.parent_net_profit,
        b.total_assets, b.total_liab, b.total_equity,
        b.current_assets, b.cash_equivalents,
        cf.cfo_net
    FROM income_statement i
    JOIN stock_info s ON i.stock_code = s.stock_code
    JOIN balance_sheet b
        ON i.stock_code = b.stock_code
        AND i.report_date = b.report_date
        AND i.report_type = b.report_type
    LEFT JOIN cash_flow_statement cf
        ON i.stock_code = cf.stock_code
        AND i.report_date = cf.report_date
        AND i.report_type = cf.report_type
    WHERE 1=1 {market_clause}
    ORDER BY i.stock_code, i.report_date
    """
    rows = db.execute(sql, params, fetch=True) or []
    scanned = len(rows)

    for r in rows:
        stock_code, mkt, rdate, rtype = r[0], r[1], r[2], r[3]
        total_revenue = _d(r[4])
        operating_revenue = _d(r[5])
        net_profit = _d(r[6])
        parent_net_profit = _d(r[7])
        total_assets = _d(r[8])
        total_liab = _d(r[9])
        total_equity = _d(r[10])
        current_assets = _d(r[11])
        cash_equiv = _d(r[12])
        cfo = _d(r[13])

        rd = str(rdate)

        # 1a. 负资产
        if total_assets is not None and total_assets < 0:
            issues.append(
                ValidationIssue(
                    stock_code=stock_code,
                    market=mkt,
                    report_date=rd,
                    check_name="negative_total_assets",
                    severity="error",
                    field_name="total_assets",
                    actual_value=str(total_assets),
                    message=f"总资产为负数: {total_assets:,.0f}",
                    suggestion="数据录入错误或持续经营问题，需核实",
                )
            )

        # 1b. 资产负债率 > 200%
        if total_assets and total_liab:
            ratio = total_liab / total_assets
            if ratio > 2.0:
                issues.append(
                    ValidationIssue(
                        stock_code=stock_code,
                        market=mkt,
                        report_date=rd,
                        check_name="debt_ratio_extreme",
                        severity="warning",
                        field_name="total_liab/total_assets",
                        actual_value=f"{ratio:.2%}",
                        expected_value="< 200%",
                        message=f"资产负债率 {ratio:.1%} 超过 200%",
                        suggestion="可能资不抵债，检查数据准确性",
                    )
                )

        # 1c. 净利润超过营收（绝对值）
        if net_profit is not None and total_revenue is not None:
            if total_revenue > 0 and net_profit > total_revenue * 1.5:
                issues.append(
                    ValidationIssue(
                        stock_code=stock_code,
                        market=mkt,
                        report_date=rd,
                        check_name="net_profit_exceeds_revenue",
                        severity="warning",
                        field_name="net_profit/total_revenue",
                        actual_value=f"净利润={net_profit:,.0f}, 营收={total_revenue:,.0f}",
                        expected_value="净利润通常不超过营收的 1.5 倍",
                        message=f"净利润({net_profit:,.0f})远超营收({total_revenue:,.0f})",
                        suggestion="可能有大额投资收益/营业外收入，需核实利润构成",
                    )
                )

        # 1d. 经营现金流与净利润严重背离
        if cfo is not None and parent_net_profit is not None:
            if parent_net_profit > 0 and cfo < 0:
                issues.append(
                    ValidationIssue(
                        stock_code=stock_code,
                        market=mkt,
                        report_date=rd,
                        check_name="cfo_negative_profit_positive",
                        severity="warning",
                        field_name="cfo_net/parent_net_profit",
                        actual_value=f"CFO={cfo:,.0f}, 净利润={parent_net_profit:,.0f}",
                        message="净利润为正但经营现金流为负",
                        suggestion="盈利质量存疑，检查应收/存货变化",
                    )
                )

        # 1e. 关键指标为零（可能缺失）
        if rtype == "annual":
            if total_revenue is not None and total_revenue == 0:
                issues.append(
                    ValidationIssue(
                        stock_code=stock_code,
                        market=mkt,
                        report_date=rd,
                        check_name="zero_revenue_annual",
                        severity="warning",
                        field_name="total_revenue",
                        actual_value="0",
                        message="年报营收为零",
                        suggestion="可能已停业/退市，或数据缺失",
                    )
                )

    return scanned


# ──────────────────────────────────────────────────────────
#  1b. 异常值检测 — 美股
# ──────────────────────────────────────────────────────────


def check_anomalies_us(issues: list[ValidationIssue]) -> int:
    """对美股做异常值检测。返回扫描行数。"""
    sql = """
    SELECT
        ui.stock_code, ui.report_date, ui.report_type,
        ui.revenues, ui.net_income,
        ub.total_assets, ub.total_liabilities, ub.total_equity,
        ub.total_current_assets, ub.cash_and_equivalents,
        uc.net_cash_from_operations
    FROM us_income_statement ui
    JOIN us_balance_sheet ub
        ON ui.stock_code = ub.stock_code
        AND ui.report_date = ub.report_date
        AND ui.report_type = ub.report_type
    LEFT JOIN us_cash_flow_statement uc
        ON ui.stock_code = uc.stock_code
        AND ui.report_date = uc.report_date
        AND ui.report_type = uc.report_type
    ORDER BY ui.stock_code, ui.report_date
    """
    rows = db.execute(sql, fetch=True) or []
    scanned = len(rows)

    for r in rows:
        stock_code, rdate, rtype = r[0], r[1], r[2]
        revenues = _d(r[3])
        net_income = _d(r[4])
        total_assets = _d(r[5])
        total_liabilities = _d(r[6])
        total_equity = _d(r[7])
        current_assets = _d(r[8])
        cash_equiv = _d(r[9])
        cfo = _d(r[10])

        rd = str(rdate)

        # 负资产
        if total_assets is not None and total_assets < 0:
            issues.append(
                ValidationIssue(
                    stock_code=stock_code,
                    market="US",
                    report_date=rd,
                    check_name="negative_total_assets",
                    severity="error",
                    field_name="total_assets",
                    actual_value=str(total_assets),
                    message=f"总资产为负数: {total_assets:,.0f}",
                    suggestion="数据录入错误或持续经营问题",
                )
            )

        # 资产负债率 > 200%
        if total_assets and total_liabilities:
            ratio = total_liabilities / total_assets
            if ratio > 2.0:
                issues.append(
                    ValidationIssue(
                        stock_code=stock_code,
                        market="US",
                        report_date=rd,
                        check_name="debt_ratio_extreme",
                        severity="warning",
                        field_name="total_liabilities/total_assets",
                        actual_value=f"{ratio:.2%}",
                        expected_value="< 200%",
                        message=f"资产负债率 {ratio:.1%} 超过 200%",
                        suggestion="可能资不抵债",
                    )
                )

        # 净利润超过营收
        if net_income is not None and revenues is not None:
            if revenues > 0 and net_income > revenues * 1.5:
                issues.append(
                    ValidationIssue(
                        stock_code=stock_code,
                        market="US",
                        report_date=rd,
                        check_name="net_income_exceeds_revenue",
                        severity="warning",
                        field_name="net_income/revenues",
                        actual_value=f"净利润={net_income:,.0f}, 营收={revenues:,.0f}",
                        message=f"净利润({net_income:,.0f})远超营收({revenues:,.0f})",
                        suggestion="可能有大额非经常性收益",
                    )
                )

        # CFO 与净利润背离
        if cfo is not None and net_income is not None:
            if net_income > 0 and cfo < 0:
                issues.append(
                    ValidationIssue(
                        stock_code=stock_code,
                        market="US",
                        report_date=rd,
                        check_name="cfo_negative_income_positive",
                        severity="warning",
                        field_name="net_cash_from_operations/net_income",
                        actual_value=f"CFO={cfo:,.0f}, 净利润={net_income:,.0f}",
                        message="净利润为正但经营现金流为负",
                        suggestion="盈利质量存疑",
                    )
                )

    return scanned


# ──────────────────────────────────────────────────────────
#  2. 逻辑一致性检查 — A 股 / 港股
# ──────────────────────────────────────────────────────────


def check_logic_cn_hk(market: str, issues: list[ValidationIssue]) -> int:
    """A 股/港股逻辑一致性检查。返回扫描行数。"""
    market_clause, params = _market_filter(market, "s")

    # 资产 = 负债 + 权益（会计等式）
    sql_balance_eq = f"""
    SELECT
        i.stock_code, s.market, i.report_date,
        b.total_assets, b.total_liab, b.total_equity,
        b.current_assets, b.cash_equivalents,
        b.minority_equity
    FROM income_statement i
    JOIN stock_info s ON i.stock_code = s.stock_code
    JOIN balance_sheet b
        ON i.stock_code = b.stock_code
        AND i.report_date = b.report_date
        AND i.report_type = b.report_type
    WHERE 1=1 {market_clause}
      AND b.total_assets IS NOT NULL
      AND b.total_liab IS NOT NULL
      AND b.total_equity IS NOT NULL
    ORDER BY i.stock_code, i.report_date
    """
    rows = db.execute(sql_balance_eq, params, fetch=True) or []
    scanned = len(rows)
    tolerance_ratio = 0.01  # 1% 容差

    for r in rows:
        stock_code, mkt, rdate = r[0], r[1], r[2]
        total_assets = _d(r[3])
        total_liab = _d(r[4])
        total_equity = _d(r[5])
        current_assets = _d(r[6])
        cash_equiv = _d(r[7])
        minority_equity = _d(r[8])
        rd = str(rdate)

        # 2a. 会计等式：资产 ≈ 负债 + 权益
        if total_assets and total_liab is not None and total_equity is not None:
            rhs = total_liab + total_equity
            if total_assets != 0:
                diff_ratio = abs(total_assets - rhs) / abs(total_assets)
                if diff_ratio > tolerance_ratio:
                    issues.append(
                        ValidationIssue(
                            stock_code=stock_code,
                            market=mkt,
                            report_date=rd,
                            check_name="balance_equation",
                            severity="error",
                            field_name="total_assets vs total_liab + total_equity",
                            actual_value=f"资产={total_assets:,.0f}, 负债+权益={rhs:,.0f}, 偏差={diff_ratio:.2%}",
                            expected_value="偏差 < 1%",
                            message=f"会计等式不平：资产({total_assets:,.0f}) ≠ 负债({total_liab:,.0f}) + 权益({total_equity:,.0f})，偏差 {diff_ratio:.2%}",
                            suggestion="检查数据源是否有遗漏科目（如少数股东权益未计入）",
                        )
                    )

        # 2b. 流动资产 >= 现金及等价物
        if current_assets is not None and cash_equiv is not None:
            if cash_equiv > current_assets and current_assets >= 0:
                issues.append(
                    ValidationIssue(
                        stock_code=stock_code,
                        market=mkt,
                        report_date=rd,
                        check_name="cash_exceeds_current_assets",
                        severity="error",
                        field_name="cash_equivalents vs current_assets",
                        actual_value=f"现金={cash_equiv:,.0f}, 流动资产={current_assets:,.0f}",
                        expected_value="现金 <= 流动资产",
                        message=f"货币资金({cash_equiv:,.0f}) > 流动资产({current_assets:,.0f})，不合逻辑",
                        suggestion="数据可能存在错误，或货币资金口径问题",
                    )
                )

    return scanned


# ──────────────────────────────────────────────────────────
#  2b. 逻辑一致性检查 — 美股
# ──────────────────────────────────────────────────────────


def check_logic_us(issues: list[ValidationIssue]) -> int:
    """美股逻辑一致性检查。返回扫描行数。"""
    sql = """
    SELECT
        ub.stock_code, ub.report_date,
        ub.total_assets, ub.total_liabilities, ub.total_equity, ub.total_equity_including_nci,
        ub.total_current_assets, ub.cash_and_equivalents
    FROM us_balance_sheet ub
    WHERE ub.total_assets IS NOT NULL
      AND ub.total_liabilities IS NOT NULL
      AND ub.total_equity IS NOT NULL
    ORDER BY ub.stock_code, ub.report_date
    """
    rows = db.execute(sql, fetch=True) or []
    scanned = len(rows)
    tolerance_ratio = 0.01

    for r in rows:
        stock_code, rdate = r[0], r[1]
        total_assets = _d(r[2])
        total_liabilities = _d(r[3])
        total_equity = _d(r[4])
        total_equity_nci = _d(r[5])
        current_assets = _d(r[6])
        cash_equiv = _d(r[7])
        rd = str(rdate)

        # 会计等式
        if total_assets and total_liabilities is not None and total_equity is not None:
            rhs = total_liabilities + total_equity
            if total_assets != 0:
                diff_ratio = abs(total_assets - rhs) / abs(total_assets)
                if diff_ratio > tolerance_ratio:
                    # 尝试用 total_equity_including_nci
                    if total_equity_nci is not None:
                        rhs2 = total_liabilities + total_equity_nci
                        diff2 = abs(total_assets - rhs2) / abs(total_assets)
                        if diff2 <= tolerance_ratio:
                            continue  # 用含 NCI 的权益就平了，跳过
                    issues.append(
                        ValidationIssue(
                            stock_code=stock_code,
                            market="US",
                            report_date=rd,
                            check_name="balance_equation",
                            severity="error",
                            field_name="total_assets vs total_liabilities + total_equity",
                            actual_value=f"资产={total_assets:,.0f}, 负债+权益={rhs:,.0f}, 偏差={diff_ratio:.2%}",
                            expected_value="偏差 < 1%",
                            message=f"会计等式不平：偏差 {diff_ratio:.2%}",
                            suggestion="检查少数股东权益是否单独记录",
                        )
                    )

        # 流动资产 >= 现金
        if current_assets is not None and cash_equiv is not None:
            if cash_equiv > current_assets and current_assets >= 0:
                issues.append(
                    ValidationIssue(
                        stock_code=stock_code,
                        market="US",
                        report_date=rd,
                        check_name="cash_exceeds_current_assets",
                        severity="error",
                        field_name="cash_and_equivalents vs total_current_assets",
                        actual_value=f"现金={cash_equiv:,.0f}, 流动资产={current_assets:,.0f}",
                        expected_value="现金 <= 流动资产",
                        message=f"现金({cash_equiv:,.0f}) > 流动资产({current_assets:,.0f})",
                        suggestion="数据可能存在错误",
                    )
                )

    return scanned


# ──────────────────────────────────────────────────────────
#  3. 跨源比对（当前状态记录）
# ──────────────────────────────────────────────────────────


def check_cross_source(market: str, issues: list[ValidationIssue]) -> int:
    """检查数据源多样性，记录为已知限制。

    目前 A 股/港股只有东方财富单一来源，美股只有 SEC EDGAR。
    将此记录为 info 级别，方便未来扩展跨源比对时追溯。
    """
    if market in ("A", "HK", ""):
        issues.append(
            ValidationIssue(
                stock_code="*",
                market="CN_A" if market == "A" else "CN_HK",
                report_date="*",
                check_name="single_source_limitation",
                severity="info",
                field_name="source",
                actual_value="eastmoney / eastmoney_hk",
                expected_value="多源交叉验证",
                message="A 股/港股数据仅来自东方财富单一源，暂无跨源比对能力",
                suggestion="未来可接入 akshare/同花顺作为第二数据源进行交叉验证",
            )
        )
    if market in ("US", ""):
        issues.append(
            ValidationIssue(
                stock_code="*",
                market="US",
                report_date="*",
                check_name="single_source_limitation",
                severity="info",
                field_name="source",
                actual_value="SEC EDGAR",
                expected_value="多源交叉验证",
                message="美股数据仅来自 SEC EDGAR 单一源",
                suggestion="未来可接入 Yahoo Finance / Financial Modeling Prep 作为第二数据源",
            )
        )
    return 0


# ──────────────────────────────────────────────────────────
#  4. 结果持久化
# ──────────────────────────────────────────────────────────


def save_results(report: ValidationReport, batch_id: str) -> int:
    """将校验结果写入 validation_results 表。

    Args:
        report: 校验报告
        batch_id: 批次 ID

    Returns:
        写入行数
    """
    if not report.issues:
        logger.info("无异常，跳过写入")
        return 0

    # 使用批量 INSERT（非 UPSERT），每次校验产生全新记录
    sql = """
        INSERT INTO validation_results
            (batch_id, stock_code, market, report_date, check_name,
             severity, field_name, actual_value, expected_value,
             message, suggestion)
        VALUES (%(batch_id)s, %(stock_code)s, %(market)s, %(report_date)s,
                %(check_name)s, %(severity)s, %(field_name)s,
                %(actual_value)s, %(expected_value)s, %(message)s, %(suggestion)s)
    """
    rows = []
    for issue in report.issues:
        rows.append(
            {
                "batch_id": batch_id,
                "stock_code": issue.stock_code,
                "market": issue.market,
                "report_date": issue.report_date
                if issue.report_date != "*"
                else "1970-01-01",
                "check_name": issue.check_name,
                "severity": issue.severity,
                "field_name": issue.field_name,
                "actual_value": issue.actual_value,
                "expected_value": issue.expected_value,
                "message": issue.message,
                "suggestion": issue.suggestion,
            }
        )

    count = 0
    with db.Connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)
            count = cur.rowcount
        conn.commit()
    logger.info("校验结果写入: %d 条", count)
    return count


def output_json(report: ValidationReport, filepath: str) -> None:
    """输出 JSON 文件。"""
    data = {
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "market": report.market,
        "total_rows_scanned": report.total_rows_scanned,
        "summary": {
            "errors": report.error_count,
            "warnings": report.warning_count,
            "info": report.info_count,
            "total": len(report.issues),
        },
        "issues": [asdict(i) for i in report.issues],
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    logger.info("JSON 报告已输出: %s (%d 条)", filepath, len(report.issues))


def output_csv(report: ValidationReport, filepath: str) -> None:
    """输出 CSV 文件。"""
    if not report.issues:
        return
    fieldnames = [
        "stock_code",
        "market",
        "report_date",
        "check_name",
        "severity",
        "field_name",
        "actual_value",
        "expected_value",
        "message",
        "suggestion",
    ]
    with open(filepath, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for issue in report.issues:
            writer.writerow(asdict(issue))
    logger.info("CSV 报告已输出: %s (%d 条)", filepath, len(report.issues))


# ──────────────────────────────────────────────────────────
#  5. 主入口
# ──────────────────────────────────────────────────────────


def run_validation(market: str = "", output: str = "") -> ValidationReport:
    """执行数据校验。

    Args:
        market: "A" | "HK" | "US" | "" (全部)
        output: "json" | "csv" | "" (仅数据库)

    Returns:
        ValidationReport
    """
    ensure_table()

    batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = ValidationReport(
        started_at=datetime.now().isoformat(),
        market=market or "ALL",
    )

    logger.info("=== 数据校验开始 [batch=%s market=%s] ===", batch_id, market or "ALL")

    # 按市场执行
    markets_to_run = []
    if market == "A":
        markets_to_run = ["A"]
    elif market == "HK":
        markets_to_run = ["HK"]
    elif market == "US":
        markets_to_run = ["US"]
    else:
        markets_to_run = ["A", "HK", "US"]

    for mkt in markets_to_run:
        logger.info("--- 市场: %s ---", mkt)

        if mkt in ("A", "HK"):
            scanned = check_anomalies_cn_hk(mkt, report.issues)
            report.total_rows_scanned += scanned
            logger.info("  异常值检测: 扫描 %d 行", scanned)

            scanned_logic = check_logic_cn_hk(mkt, report.issues)
            report.total_rows_scanned += scanned_logic
            logger.info("  逻辑一致性: 扫描 %d 行", scanned_logic)

        elif mkt == "US":
            scanned = check_anomalies_us(report.issues)
            report.total_rows_scanned += scanned
            logger.info("  异常值检测: 扫描 %d 行", scanned)

            scanned_logic = check_logic_us(report.issues)
            report.total_rows_scanned += scanned_logic
            logger.info("  逻辑一致性: 扫描 %d 行", scanned_logic)

        # 跨源比对
        check_cross_source(mkt, report.issues)

    # 汇总
    report.finished_at = datetime.now().isoformat()
    logger.info("=== 校验完成 ===")
    logger.info("  扫描行数: %d", report.total_rows_scanned)
    logger.info(
        "  错误: %d, 警告: %d, 信息: %d",
        report.error_count,
        report.warning_count,
        report.info_count,
    )

    # 持久化
    save_results(report, batch_id)

    # 输出文件
    if output == "json":
        filepath = f"data/validation_{batch_id}.json"
        output_json(report, filepath)
    elif output == "csv":
        filepath = f"data/validation_{batch_id}.csv"
        output_csv(report, filepath)

    return report


# ── scheduler 集成入口 ────────────────────────────────────


def run_after_sync(market: str = "") -> dict:
    """供 scheduler.py 同步完成后调用的入口。

    Returns:
        {"success": True, "errors": int, "warnings": int, "info": int}
    """
    try:
        report = run_validation(market=market)
        return {
            "success": True,
            "errors": report.error_count,
            "warnings": report.warning_count,
            "info": report.info_count,
        }
    except Exception as exc:
        logger.error("校验执行失败: %s", exc)
        return {"success": False, "error": str(exc)}


# ── CLI ────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="财务数据校验引擎")
    parser.add_argument(
        "--market",
        choices=["A", "HK", "US"],
        default="",
        help="按市场筛选: A(A股), HK(港股), US(美股)，默认全部",
    )
    parser.add_argument(
        "--output",
        choices=["json", "csv"],
        default="",
        help="额外输出格式: json 或 csv",
    )
    args = parser.parse_args()

    report = run_validation(market=args.market, output=args.output)

    # 打印摘要
    print(f"\n{'=' * 60}")
    print(f"  校验报告 — {report.market}")
    print(f"{'=' * 60}")
    print(f"  扫描行数: {report.total_rows_scanned:,}")
    print(f"  错误:     {report.error_count}")
    print(f"  警告:     {report.warning_count}")
    print(f"  信息:     {report.info_count}")

    if report.error_count > 0 or report.warning_count > 0:
        print(f"\n  TOP 问题:")
        from collections import Counter

        check_counts = Counter(
            i.check_name for i in report.issues if i.severity in ("error", "warning")
        )
        for name, cnt in check_counts.most_common(10):
            print(f"    {name}: {cnt} 条")

    print(f"{'=' * 60}\n")

    db.close_pool()


if __name__ == "__main__":
    main()
