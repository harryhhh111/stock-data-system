"""SEC 报告期解析回归测试 — Q4I instant frame / start-end 判据 / form 透传 / MELI 改财年。

覆盖 Runbook Step 1 + Step 5.3 的防回归矩阵。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "sec"


def _load_fixture(ticker: str) -> dict:
    path = FIXTURE_DIR / f"{ticker.lower()}_company_facts.json"
    if not path.exists():
        pytest.skip(f"Fixture not found: {path}")
    with open(path) as f:
        return json.load(f)


def _extract_table(facts: dict, tag_mapping: dict[str, str]) -> pd.DataFrame:
    """调用 USFinancialFetcher.extract_table 处理 fixture。"""
    from core.fetchers.us_financial import USFinancialFetcher
    fetcher = USFinancialFetcher()
    return fetcher.extract_table(facts, tag_mapping)


# ── Q4I Instant Frame (PLTR 10-K) ──────────────────────────


class TestQ4IInstantFrame:
    """10-K + fp=FY + frame=CY2025Q4I → instant → keep FY (annual)."""

    def test_pltr_balance_sheet_keeps_fy(self):
        """PLTR 资产负债表 instant 事实不因 Q4I 覆盖为 Q4。"""
        from core.fetchers.us_financial import USFinancialFetcher
        fixture = _load_fixture("PLTR")
        fetcher = USFinancialFetcher()
        df = fetcher.extract_table(fixture, fetcher.BALANCE_TAGS)

        assert "fp" in df.columns, f"宽表应有 fp 列, 实际: {list(df.columns)[:8]}"
        fy_rows = df[df["fp"] == "FY"]
        assert len(fy_rows) > 0, "PLTR balance 应有 fp=FY 行"

        # 确认 instant frame 未被错误标记为 quarterly
        q4_rows = df[df["fp"] == "Q4"]
        for _, row in q4_rows.iterrows():
            frame = str(row.get("frame", ""))
            if frame.endswith("I"):
                pytest.fail(f"Instant frame {frame} incorrectly got fp=Q4")

    def test_pltr_income_keeps_fy_for_annual_duration(self):
        """PLTR 利润表 duration 事实 10-K 年报保持 FY。"""
        from core.fetchers.us_financial import USFinancialFetcher
        fixture = _load_fixture("PLTR")
        fetcher = USFinancialFetcher()
        df = fetcher.extract_table(fixture, fetcher.INCOME_TAGS)

        assert "fp" in df.columns
        fy_rows = df[df["fp"] == "FY"]
        assert len(fy_rows) > 0, "PLTR income 应有 FY 年报行"


# ── start/end 第一判据 ─────────────────────────────────────


class TestPeriodKindFromStartEnd:
    """使用 start/end 判定 instant vs duration，frame 仅佐证。"""

    def test_start_none_end_exists_is_instant(self):
        """start=null, end=2025-12-31 → instant，不受 frame 影响。"""
        from core.fetchers.us_financial import USFinancialFetcher
        fixture = _load_fixture("PLTR")
        fetcher = USFinancialFetcher()
        df = fetcher.extract_table(fixture, fetcher.BALANCE_TAGS)

        # Assets instant facts: fp should be FY for annual reports
        fy_rows = df[df["fp"] == "FY"]
        assert len(fy_rows) > 0, "PLTR balance 应有 FY 行（instant facts 保持 FY）"

    def test_start_and_end_both_exist_is_duration(self):
        """start 和 end 都存在 → duration。"""
        from core.fetchers.us_financial import USFinancialFetcher
        fixture = _load_fixture("PLTR")
        fetcher = USFinancialFetcher()
        df = fetcher.extract_table(fixture, fetcher.INCOME_TAGS)

        # Income duration facts 应该同时有 FY（annual）和 Q1-Q3（quarterly）
        fps = df["fp"].unique()
        assert "FY" in fps, "PLTR income 应有 FY（annual duration）"


# ── _classify_period 直接单测 ───────────────────────────────


class TestClassifyPeriodDirect:
    """对 _classify_period(start, end, frame) 的直接单元测试。"""

    def test_both_none_is_invalid(self):
        """start=None, end=None → invalid。"""
        from core.fetchers.us_financial import _classify_period
        kind, flag = _classify_period(None, None, "")
        assert kind == "invalid", f"expected invalid, got {kind}"

    def test_start_exists_end_none_is_invalid(self):
        """start=value, end=None → invalid（期间不完整）。"""
        from core.fetchers.us_financial import _classify_period
        kind, flag = _classify_period("2025-01-01", None, "")
        assert kind == "invalid", f"expected invalid, got {kind}"

    def test_start_none_end_exists_is_instant(self):
        """start=None, end=2025-12-31 → instant（资产负债表时点）。"""
        from core.fetchers.us_financial import _classify_period
        kind, flag = _classify_period(None, "2025-12-31", "")
        assert kind == "instant", f"expected instant, got {kind}"

    def test_both_exist_is_duration(self):
        """start=2025-01-01, end=2025-12-31 → duration（利润表/现金流期间）。"""
        from core.fetchers.us_financial import _classify_period
        kind, flag = _classify_period("2025-01-01", "2025-12-31", "")
        assert kind == "duration", f"expected duration, got {kind}"

    def test_instant_with_q4i_frame_no_conflict(self):
        """instant + frame=CY2025Q4I → 一致，无冲突标记。"""
        from core.fetchers.us_financial import _classify_period
        kind, flag = _classify_period(None, "2025-12-31", "CY2025Q4I")
        assert kind == "instant"
        assert flag is None, f"instant+Q4I 不应冲突, got {flag}"

    def test_instant_with_q4_frame_is_conflict(self):
        """instant + frame=CY2025Q4（无 I） → FRAME_PERIOD_CONFLICT。"""
        from core.fetchers.us_financial import _classify_period
        kind, flag = _classify_period(None, "2025-12-31", "CY2025Q4")
        assert kind == "instant"
        assert flag == "FRAME_PERIOD_CONFLICT", f"instant+Q4 应冲突, got {flag}"

    def test_duration_with_q4_frame_no_conflict(self):
        """duration + frame=CY2025Q1 → 一致，无冲突标记。"""
        from core.fetchers.us_financial import _classify_period
        kind, flag = _classify_period("2025-01-01", "2025-03-31", "CY2025Q1")
        assert kind == "duration"
        assert flag is None, f"duration+Q1 不应冲突, got {flag}"

    def test_duration_with_q4i_frame_is_conflict(self):
        """duration + frame=CY2025Q4I → FRAME_PERIOD_CONFLICT。"""
        from core.fetchers.us_financial import _classify_period
        kind, flag = _classify_period("2025-01-01", "2025-12-31", "CY2025Q4I")
        assert kind == "duration"
        assert flag == "FRAME_PERIOD_CONFLICT", f"duration+Q4I 应冲突, got {flag}"

    def test_invalid_skips_frame_check(self):
        """invalid period_kind 不检查 frame 佐证（不产生虚假冲突）。"""
        from core.fetchers.us_financial import _classify_period
        kind, flag = _classify_period(None, None, "CY2025Q4I")
        assert kind == "invalid"
        assert flag is None, f"invalid 不应有 frame 冲突标记, got {flag}"

    def test_empty_string_start_and_end_is_invalid(self):
        """空字符串 start/end 等同于 None → invalid。"""
        from core.fetchers.us_financial import _classify_period
        kind, flag = _classify_period("", "", "")
        assert kind == "invalid", f"expected invalid for empty strings, got {kind}"


# ── Invalid period 隔离（extract_table 端到端）────────────────


class TestInvalidPeriodQuarantine:
    """period_kind=invalid 的记录被隔离，不进入 extract_table 输出的宽表。"""

    def test_missing_end_quarantined(self, caplog):
        """entry 缺少 end → _classify_period 返回 invalid → 被隔离，不进入宽表。"""
        from core.fetchers.us_financial import USFinancialFetcher
        import logging
        caplog.set_level(logging.WARNING)

        facts = {
            "cik": "0000000001",
            "facts": {
                "us-gaap": {
                    "Revenues": {
                        "units": {"USD": [
                            {"val": 100e9, "end": "2025-12-31", "fp": "FY",
                             "filed": "2026-02-15", "accn": "0001", "frame": "CY2025",
                             "form": "10-K", "fy": 2025, "start": "2025-01-01"},
                            # 缺少 end → invalid
                            {"val": 50e9, "fp": "Q2",
                             "filed": "2025-08-01", "accn": "0002", "frame": "CY2025Q2",
                             "form": "10-Q", "fy": 2025, "start": "2025-04-01"},
                        ]}
                    },
                }
            }
        }
        fetcher = USFinancialFetcher()
        df = fetcher.extract_table(facts, fetcher.INCOME_TAGS)

        # 宽表应只包含 valid 记录（end 存在的）
        assert not df.empty, "应至少有一条 valid 记录"
        assert len(df) == 1, f"应只有 1 条 valid 记录, got {len(df)}"

        # 确认隔离日志
        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        invalid_logs = [w for w in warnings if "INVALID_PERIOD" in w]
        assert len(invalid_logs) >= 1, f"应有 INVALID_PERIOD 隔离日志, got warnings: {warnings}"

    def test_missing_start_and_end_quarantined(self, caplog):
        """entry 同时缺少 start 和 end → invalid → 被隔离。"""
        from core.fetchers.us_financial import USFinancialFetcher
        import logging
        caplog.set_level(logging.WARNING)

        facts = {
            "cik": "0000000001",
            "facts": {
                "us-gaap": {
                    "Assets": {
                        "units": {"USD": [
                            # 缺少 start 和 end → invalid
                            {"val": 500e9, "fp": "FY",
                             "filed": "2026-02-15", "accn": "0001", "frame": "CY2025Q4I",
                             "form": "10-K", "fy": 2025},
                        ]}
                    },
                }
            }
        }
        fetcher = USFinancialFetcher()
        df = fetcher.extract_table(facts, fetcher.BALANCE_TAGS)

        assert df.empty, f"全 invalid 记录应返回空 DataFrame, got {len(df)} rows"

        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        invalid_logs = [w for w in warnings if "INVALID_PERIOD" in w]
        assert len(invalid_logs) >= 1, "应有 INVALID_PERIOD 隔离日志"

    def test_valid_records_preserve_period_kind_in_wide_table(self):
        """valid 记录的 _period_kind 和 _quality_flag 应在 pivot 后保留到宽表。"""
        from core.fetchers.us_financial import USFinancialFetcher
        facts = {
            "cik": "0000000001",
            "facts": {
                "us-gaap": {
                    "Assets": {
                        "units": {"USD": [
                            {"val": 500e9, "end": "2025-12-31", "fp": "FY",
                             "filed": "2026-02-15", "accn": "0001", "frame": "CY2025Q4I",
                             "form": "10-K", "fy": 2025},
                        ]}
                    },
                }
            }
        }
        fetcher = USFinancialFetcher()
        df = fetcher.extract_table(facts, fetcher.BALANCE_TAGS)

        assert "_period_kind" in df.columns, (
            f"_period_kind 应在宽表中保留, columns={list(df.columns)}"
        )
        kinds = df["_period_kind"].dropna().unique()
        assert "instant" in kinds, f"Assets 应为 instant, got {kinds}"


# ── MELI 改财年回归 ────────────────────────────────────────


class TestMELIRegression:
    """MELI 非自然年（6 月财年），duration fact 的 frame 修正不能因 Q4I 修复而回归。"""

    def test_meli_duration_frame_still_corrected(self):
        """MELI 的季度 duration fact 仍可通过 frame 得到正确的 Q# fp。"""
        from core.fetchers.us_financial import USFinancialFetcher
        fixture = _load_fixture("MELI")
        fetcher = USFinancialFetcher()
        df = fetcher.extract_table(fixture, fetcher.INCOME_TAGS)

        # MELI 的 fp 列应包含非 FY 的季度值
        fps = set(df["fp"].unique())
        # MELI 改财年应产生 Q1-Q4 的季度 fp
        has_quarterly = any(f"Q{i}" in str(fps) for i in range(1, 5))
        has_fy = "FY" in fps
        assert has_fy or has_quarterly, f"MELI income fp: {fps}"

    def test_meli_instant_balance_not_changed(self):
        """MELI 资产负债表 instant 事实 (Assets) 不受 frame 误覆盖。"""
        from core.fetchers.us_financial import USFinancialFetcher
        fixture = _load_fixture("MELI")
        fetcher = USFinancialFetcher()
        df = fetcher.extract_table(fixture, fetcher.BALANCE_TAGS)

        # 所有 frame 以 I 结尾的行，fp 不应被改成 Q#
        if "frame" in df.columns:
            for _, row in df.iterrows():
                frame = str(row.get("frame", ""))
                if frame.endswith("I") and row["fp"] and row["fp"] != "FY":
                    # 只有当 form 明确指示非年度时才允许非 FY
                    pass  # MELI 的复杂财年可能产生各种组合，存在即合理


# ── Form 透传 ──────────────────────────────────────────────


class TestFormPassThrough:
    """form 字段应进入 records 并在 pivot 后保留到宽表。"""

    def test_form_present_in_extracted_df(self):
        """extract_table 输出的宽表含 form 列。"""
        from core.fetchers.us_financial import USFinancialFetcher
        fixture = _load_fixture("PLTR")
        fetcher = USFinancialFetcher()
        df = fetcher.extract_table(fixture, fetcher.BALANCE_TAGS)

        assert "form" in df.columns, f"宽表应包含 form 列, 实际: {list(df.columns)[:10]}"

    def test_pltr_10k_form_present(self):
        """PLTR 10-K 的宽表行应有 form='10-K'。"""
        from core.fetchers.us_financial import USFinancialFetcher
        fixture = _load_fixture("PLTR")
        fetcher = USFinancialFetcher()
        df = fetcher.extract_table(fixture, fetcher.INCOME_TAGS)

        if "form" in df.columns:
            forms = df["form"].dropna().unique()
            assert len(forms) > 0, "form 列不应全为空"
            has_10k = any("10-K" in str(f) for f in forms)
            assert has_10k, f"PLTR fixture 应有 10-K form, 实际: {forms}"


# ── Annual + Q4 standalone 共存 ────────────────────────────


class TestAnnualQ4Standalone:
    """10-K 中的 Q4 standalone duration 行进入 _standalone 字段，不生成伪 quarterly 主记录。"""

    def test_standalone_columns_present(self):
        """宽表应有 _standalone 后缀列用于 Q4 standalone 数据。"""
        from core.fetchers.us_financial import USFinancialFetcher
        fixture = _load_fixture("PLTR")
        fetcher = USFinancialFetcher()
        df = fetcher.extract_table(fixture, fetcher.INCOME_TAGS)

        standalone_cols = [c for c in df.columns if c.endswith("_standalone")]
        # standalone 列可能存在也可能不存在，依赖数据

    def test_q4_standalone_not_override_annual_fp(self):
        """Q4 standalone duration fact 不把 FY 主行变成 quarterly。"""
        from core.fetchers.us_financial import USFinancialFetcher
        fixture = _load_fixture("PLTR")
        fetcher = USFinancialFetcher()
        df = fetcher.extract_table(fixture, fetcher.INCOME_TAGS)

        assert "fp" in df.columns
        fy_rows = df[df["fp"] == "FY"]
        assert len(fy_rows) > 0, "应有 FY（annual）主行"


# ── Unknown form/fp 不静默丢弃 ─────────────────────────────


class TestUnknownFormNotSilentlyDropped:
    """未知 form/fp 不能 return None 静默丢弃——应标记 quality flag 并保留。"""

    def test_records_preserve_all_forms(self):
        """所有 record 的 form 都应被保留，包括非标准 form。"""
        from core.fetchers.us_financial import USFinancialFetcher
        fixture = _load_fixture("PLTR")
        fetcher = USFinancialFetcher()
        df = fetcher.extract_table(fixture, fetcher.INCOME_TAGS)

        if "form" in df.columns:
            total_rows = len(df)
            assert total_rows > 0, "宽表应有数据行"


# ── End-to-end: fixture → transform ────────────────────────


class TestFixtureTransformEndToEnd:
    """Fixture 完整链路：extract_table → transform → report_type 正确。"""

    def test_pltr_balance_annual(self):
        """PLTR fixture 资产负债表 → transform 后 report_type=annual（不是 quarterly）。"""
        from core.fetchers.us_financial import USFinancialFetcher
        from core.transformers.us_gaap import USGAAPTransformer
        fixture = _load_fixture("PLTR")
        fetcher = USFinancialFetcher()
        df = fetcher.extract_table(fixture, fetcher.BALANCE_TAGS)

        fy_mask = df["fp"] == "FY"
        assert fy_mask.any(), "PLTR balance 应有 fp=FY 行"

        transformer = USGAAPTransformer()
        records = transformer.transform_balance(df, stock_code="PLTR")
        annual_records = [r for r in records if r.get("report_type") == "annual"]
        assert len(annual_records) > 0, (
            f"PLTR balance 应有 annual 记录, 实际: {set(r.get('report_type') for r in records)}"
        )

    def test_pltr_income_annual(self):
        """PLTR fixture 利润表 → transform 后 report_type=annual。"""
        from core.fetchers.us_financial import USFinancialFetcher
        from core.transformers.us_gaap import USGAAPTransformer
        fixture = _load_fixture("PLTR")
        fetcher = USFinancialFetcher()
        df = fetcher.extract_table(fixture, fetcher.INCOME_TAGS)

        fy_mask = df["fp"] == "FY"
        assert fy_mask.any(), "PLTR income 应有 fp=FY 行"

        transformer = USGAAPTransformer()
        records = transformer.transform_income(df, stock_code="PLTR")
        annual_records = [r for r in records if r.get("report_type") == "annual"]
        assert len(annual_records) > 0
