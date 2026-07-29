"""美股财务数据全链路确定性审计。

检查链路：
SEC Company Facts 快照 -> 版本层 -> 旧宽表/TTM/估值消费者。
语义不明的差异只进入 AI 复核队列，本模块不调用模型。
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from db import execute


OFFICIAL_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"}


@dataclass(frozen=True)
class ChainFinding:
    stock_code: str
    stage: str
    code: str
    severity: str
    message: str
    field: str | None = None
    report_date: str | None = None


def _source_horizon(raw_data: dict[str, Any]) -> tuple[date | None, date | None]:
    """返回快照中正式定期报告的最大 (report_date, filed_date)。"""
    report_dates: list[date] = []
    filed_dates: list[date] = []
    for namespace in raw_data.get("facts", {}).values():
        for fact in namespace.values():
            for units in fact.get("units", {}).values():
                for item in units:
                    if str(item.get("form") or "").upper() not in OFFICIAL_FORMS:
                        continue
                    try:
                        if item.get("end"):
                            report_dates.append(date.fromisoformat(str(item["end"])[:10]))
                        if item.get("filed"):
                            filed_dates.append(date.fromisoformat(str(item["filed"])[:10]))
                    except ValueError:
                        continue
    return (
        max(report_dates) if report_dates else None,
        max(filed_dates) if filed_dates else None,
    )


class USFinancialChainAuditor:
    """组合现有对账器和版本层覆盖检查。"""

    def audit(self, stock_codes: list[str] | None = None) -> dict[str, Any]:
        stocks = self._stock_scope(stock_codes)
        findings = self._audit_ingest_horizon(stocks)
        comparison_summary, comparison_findings = self._audit_consumers(stocks)
        findings.extend(comparison_findings)
        counts = Counter(item.severity for item in findings)
        ai_queue = [
            asdict(item) for item in findings
            if item.code == "UNEXPLAINED_DIFFERENCE"
        ]
        return {
            "schema_version": "us_financial_chain_audit_v1",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "stock_count": len(stocks),
            "stocks": stocks,
            "summary": {
                "finding_count": len(findings),
                "blocker": counts["blocker"],
                "warning": counts["warning"],
                "info": counts["info"],
                "ai_review_count": len(ai_queue),
            },
            "comparison": comparison_summary,
            "findings": [asdict(item) for item in findings],
            "ai_review_queue": ai_queue,
        }

    @staticmethod
    def _stock_scope(stock_codes: list[str] | None) -> list[str]:
        if stock_codes:
            return sorted({
                str(code).strip().upper()
                for code in stock_codes
                if str(code).strip()
            })
        rows = execute(
            "SELECT stock_code FROM stock_info WHERE market='US' ORDER BY stock_code",
            fetch=True,
        ) or []
        return [row[0] for row in rows]

    def _audit_ingest_horizon(self, stocks: list[str]) -> list[ChainFinding]:
        if not stocks:
            return []
        snapshot_rows = execute(
            """
            WITH activity AS (
                SELECT snapshot_id, MAX(activity_at) AS activity_at
                FROM (
                    SELECT snapshot_id, fetched_at AS activity_at
                    FROM raw_snapshot_observation
                    UNION ALL
                    SELECT snapshot_id, started_at AS activity_at
                    FROM us_ingest_run
                ) events
                GROUP BY snapshot_id
            ),
            ranked AS (
                SELECT rsv.stock_code, rsv.snapshot_id,
                       (rsv.raw_data->'facts' ? 'us-gaap') AS has_us_gaap,
                       ROW_NUMBER() OVER (
                           PARTITION BY rsv.stock_code
                           ORDER BY (activity.activity_at IS NOT NULL) DESC,
                                    COALESCE(activity.activity_at, rsv.fetched_at) DESC,
                                    rsv.snapshot_id DESC
                       ) AS rank
                FROM raw_snapshot_version rsv
                LEFT JOIN activity ON activity.snapshot_id=rsv.snapshot_id
                WHERE rsv.stock_code = ANY(%s)
                  AND rsv.data_type='company_facts'
            )
            SELECT stock_code, snapshot_id, has_us_gaap
            FROM ranked
            WHERE rank=1
            """,
            (stocks,),
            fetch=True,
        ) or []
        snapshots = {row[0]: (row[1], bool(row[2])) for row in snapshot_rows}
        info_rows = execute(
            "SELECT stock_code, stock_name FROM stock_info "
            "WHERE stock_code = ANY(%s) AND market='US'",
            (stocks,),
            fetch=True,
        ) or []
        stock_names = {row[0]: str(row[1] or "") for row in info_rows}

        filing_rows = execute(
            """
            SELECT stock_code, MAX(report_date), MAX(filed_date)
            FROM us_filing
            WHERE stock_code = ANY(%s)
              AND form = ANY(%s)
            GROUP BY stock_code
            """,
            (stocks, sorted(OFFICIAL_FORMS)),
            fetch=True,
        ) or []
        filing_horizon = {row[0]: (row[1], row[2]) for row in filing_rows}

        fact_rows = execute(
            """
            SELECT stock_code, MAX(report_date), MAX(filed_date)
            FROM us_financial_fact_version
            WHERE stock_code = ANY(%s)
            GROUP BY stock_code
            """,
            (stocks,),
            fetch=True,
        ) or []
        fact_horizon = {row[0]: (row[1], row[2]) for row in fact_rows}

        wide_rows = execute(
            """
            SELECT stock_code, MAX(report_date) FROM (
                SELECT stock_code, report_date FROM us_income_statement
                WHERE stock_code = ANY(%s)
                UNION ALL
                SELECT stock_code, report_date FROM us_balance_sheet
                WHERE stock_code = ANY(%s)
                UNION ALL
                SELECT stock_code, report_date FROM us_cash_flow_statement
                WHERE stock_code = ANY(%s)
            ) statements GROUP BY stock_code
            """,
            (stocks, stocks, stocks),
            fetch=True,
        ) or []
        wide_horizon = {row[0]: row[1] for row in wide_rows}

        run_rows = execute(
            """
            WITH selected AS (
                SELECT UNNEST(%s::bigint[]) AS snapshot_id
            ),
            ranked_runs AS (
                SELECT run.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY run.snapshot_id
                           ORDER BY run.run_id DESC
                       ) AS rank
                FROM us_ingest_run run
                JOIN selected USING(snapshot_id)
            ),
            run_agg AS (
                SELECT snapshot_id,
                       COUNT(*) FILTER (WHERE status='success') AS success_runs,
                       COALESCE(SUM(facts_conflicted), 0) AS conflicts,
                       COALESCE(SUM(facts_reviewed), 0) AS staged
                FROM ranked_runs
                WHERE rank <= 3
                GROUP BY snapshot_id
            ),
            fact_agg AS (
                SELECT fact.source_snapshot_id AS snapshot_id,
                       COUNT(*) AS version_facts
                FROM us_financial_fact_version fact
                JOIN selected ON selected.snapshot_id=fact.source_snapshot_id
                GROUP BY fact.source_snapshot_id
            )
            SELECT rsv.stock_code,
                   COALESCE(run_agg.success_runs, 0),
                   COALESCE(run_agg.conflicts, 0),
                   COALESCE(run_agg.staged, 0),
                   COALESCE(fact_agg.version_facts, 0)
            FROM selected
            JOIN raw_snapshot_version rsv USING(snapshot_id)
            LEFT JOIN run_agg USING(snapshot_id)
            LEFT JOIN fact_agg USING(snapshot_id)
            """,
            ([value[0] for value in snapshots.values()] or [-1],),
            fetch=True,
        ) or []
        run_stats = {row[0]: (row[1], row[2], row[3], row[4]) for row in run_rows}

        findings: list[ChainFinding] = []
        for stock in stocks:
            snapshot = snapshots.get(stock)
            if snapshot is None:
                stock_name = stock_names.get(stock, "")
                is_fund = "ETF" in stock_name.upper() or " FUND" in stock_name.upper()
                findings.append(ChainFinding(
                    stock,
                    "raw_snapshot",
                    "UNSUPPORTED_FUND" if is_fund else "SNAPSHOT_MISSING",
                    "info" if is_fund else "blocker",
                    (
                        "基金/ETF 不适用普通公司 SEC 财务链路"
                        if is_fund else "没有 SEC Company Facts 快照"
                    ),
                ))
                continue
            _snapshot_id, has_us_gaap = snapshot
            filing_report, filing_filed = filing_horizon.get(stock, (None, None))
            fact_report, fact_filed = fact_horizon.get(stock, (None, None))
            wide_report = wide_horizon.get(stock)
            success_runs, conflicts, staged, version_facts = run_stats.get(
                stock, (0, 0, 0, 0),
            )

            if success_runs < 3 and version_facts == 0:
                if not has_us_gaap:
                    findings.append(ChainFinding(
                        stock,
                        "raw_to_version",
                        "UNSUPPORTED_NON_US_GAAP",
                        "warning",
                        "SEC 快照仅含 IFRS/非 US-GAAP facts，当前解析器不支持",
                    ))
                else:
                    findings.append(ChainFinding(
                        stock, "raw_to_version", "INGEST_INCOMPLETE", "blocker",
                        f"最新快照只有 {success_runs}/3 个成功报表解析运行",
                    ))
            elif success_runs < 3:
                findings.append(ChainFinding(
                    stock, "raw_to_version", "LEGACY_INGEST_NO_RUN_AUDIT", "info",
                    f"历史回填快照有 {version_facts} 条版本事实，但没有在线 ingest run",
                ))
            if filing_filed and (not fact_filed or fact_filed < filing_filed):
                findings.append(ChainFinding(
                    stock, "raw_to_version", "VERSION_FILED_LAG", "blocker",
                    f"filing 层最新 filed_date={filing_filed}，版本层={fact_filed}",
                ))
            # Company Facts 同一 filing 还包含封面页 shares-outstanding
            # 等晚于财报期末的 instant 日期，不能直接用 raw max(end) 代表
            # report_date。us_filing 已按 accession/fp 推导本期日，以它为准。
            if filing_report and (not fact_report or fact_report < filing_report):
                findings.append(ChainFinding(
                    stock, "raw_to_version", "VERSION_REPORT_LAG", "blocker",
                    f"filing 层最新 report_date={filing_report}，版本层={fact_report}",
                ))
            if filing_report and (not wide_report or wide_report < filing_report):
                findings.append(ChainFinding(
                    stock, "version_to_wide", "WIDE_REPORT_LAG", "blocker",
                    f"filing 层最新 report_date={filing_report}，宽表={wide_report}",
                ))
            if conflicts:
                findings.append(ChainFinding(
                    stock, "raw_to_version", "FACT_CONFLICT", "warning",
                    f"最新快照产生 {conflicts} 条冲突事实",
                ))
            if staged:
                findings.append(ChainFinding(
                    stock, "raw_to_version", "FACT_STAGED", "warning",
                    f"最新快照有 {staged} 条待解释事实",
                ))
        return findings

    @staticmethod
    def _audit_consumers(
        stocks: list[str],
    ) -> tuple[dict[str, Any], list[ChainFinding]]:
        from scripts.compare_old_new_financials import Reason, run_comparison

        comparison = run_comparison(stocks).current_snapshot()
        reason_counts = comparison.stats_by_reason()
        findings: list[ChainFinding] = []
        for row in comparison.rows:
            if row.reason == Reason.SAME:
                continue
            if row.reason == Reason.UNEXPLAINED:
                severity = "blocker"
                code = "UNEXPLAINED_DIFFERENCE"
            elif row.reason == Reason.MISSING_MAPPING:
                severity = "warning"
                code = "MISSING_MAPPING"
            else:
                severity = "info"
                code = row.reason
            findings.append(ChainFinding(
                row.stock_code,
                "version_to_consumer",
                code,
                severity,
                f"旧消费者={row.old_value}，版本口径={row.new_value}",
                field=row.field,
                report_date=str(row.report_date),
            ))
        return {
            "row_count": len(comparison.rows),
            "reason_counts": reason_counts,
            "stocks_without_version_facts": comparison.stocks_without_version_facts,
        }, findings


def write_chain_audit(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "chain_audit.json"
    md_path = output_dir / "summary.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    summary = report["summary"]
    lines = [
        "# 美股财务全链路审核",
        "",
        f"- 股票数：{report['stock_count']}",
        f"- Blocker：{summary['blocker']}",
        f"- Warning：{summary['warning']}",
        f"- Info：{summary['info']}",
        f"- AI 待复核：{summary['ai_review_count']}",
        "",
        "## 异常",
        "",
        "| 股票 | 阶段 | 代码 | 严重度 | 字段 | 说明 |",
        "|---|---|---|---|---|---|",
    ]
    for item in report["findings"]:
        lines.append(
            f"| {item['stock_code']} | {item['stage']} | {item['code']} | "
            f"{item['severity']} | {item.get('field') or ''} | {item['message']} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path
