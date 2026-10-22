"""US universe 登记/分类自动化测试（离线：mock 身份解析与 SEC 响应）。

方案：docs/core/US_UNIVERSE_REGISTRY_AUTOMATION_PLAN.md §4 验收清单。
不访问真实 SEC，不读写仓库内的真实台账 CSV（全部用 tmp_path 副本）。
"""
import csv
from datetime import date, timedelta
from pathlib import Path

import pytest

from core import sec_client
from core import us_universe_registry as reg
from core.us_security_identity import SymbolRow

TODAY = date(2026, 10, 22)


def make_symbols(rows):
    return [SymbolRow(ticker=t, canonical_stock_code=c, cik=k, symbol_role=r)
            for t, c, k, r in rows]


@pytest.fixture
def csv_path(tmp_path):
    return tmp_path / "index_only.csv"


@pytest.fixture
def supplement_csv(tmp_path):
    return tmp_path / "supplement.csv"


@pytest.fixture
def mock_identity(monkeypatch):
    """默认身份表：GOOD1→CIK 0001111111，GOOD2→CIK 0002222222。"""
    symbols = make_symbols([
        ("GOOD1", "GOOD1", "0001111111", "current"),
        ("GOOD2", "GOOD2", "0002222222", "current"),
        ("CONFLICT", "AAA", "0003333333", "current"),
        ("CONFLICT", "BBB", "0004444444", "legacy"),
        ("MISMATCH", "MISMATCH", "0005555555", "current"),
    ])
    stock_cik = {"MISMATCH": "0009999999"}  # 与身份行不一致
    monkeypatch.setattr(reg, "_load_identity", lambda: (symbols, stock_cik))
    return symbols


@pytest.fixture
def mock_sec(monkeypatch):
    """companyfacts 响应表：CIK → status；未列出抛 SECWaitTimeout。"""
    calls = []
    table = {"0001111111": 200, "0002222222": 200, "0003333333": 404}

    class Resp:
        def __init__(self, status):
            self.status_code = status

    def fake_get(url, **kw):
        cik = url.split("CIK")[1].split(".")[0]
        calls.append(cik)
        status = table.get(cik)
        if status is None:
            raise sec_client.SECWaitTimeout("test: no route")
        return Resp(status)

    monkeypatch.setattr(sec_client, "get", fake_get)
    return calls


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


class TestIndexOnlyAutoRegister:
    def test_pass_writes_auto_row(self, csv_path, mock_identity, mock_sec):
        result = reg.auto_register_index_only(["GOOD1"], csv_path=csv_path,
                                              today=TODAY)
        assert result == {"registered": ["GOOD1"], "failed": []}
        rows = read_csv(csv_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["stock_code"] == "GOOD1"
        assert row["registered_by"] == "auto"
        assert "CIK0001111111" in row["evidence_ref"]
        assert "companyfacts HTTP 200" in row["evidence_ref"]
        assert str(TODAY) in row["evidence_ref"]
        assert row["first_confirmed"] == "2026-10-22"
        assert row["review_by"] == (TODAY + timedelta(days=90)).isoformat()

    def test_no_identity_mapping_blocked(self, csv_path, mock_identity, mock_sec):
        result = reg.auto_register_index_only(["XE"], csv_path=csv_path,
                                              today=TODAY)
        assert result["registered"] == []
        assert result["failed"][0][0] == "XE"
        assert "无映射" in result["failed"][0][1]
        assert not csv_path.exists() or read_csv(csv_path) == []
        assert mock_sec == []          # 无 CIK 不发 SEC 请求

    def test_identity_conflict_blocked(self, csv_path, mock_identity, mock_sec):
        result = reg.auto_register_index_only(["CONFLICT"], csv_path=csv_path,
                                              today=TODAY)
        assert result["registered"] == []
        assert "身份冲突" in result["failed"][0][1]
        assert mock_sec == []

    def test_cik_mismatch_blocked(self, csv_path, mock_identity, mock_sec):
        result = reg.auto_register_index_only(["MISMATCH"], csv_path=csv_path,
                                              today=TODAY)
        assert result["registered"] == []
        assert "不一致" in result["failed"][0][1]
        assert mock_sec == []

    def test_companyfacts_404_blocked(self, csv_path, mock_identity,
                                      mock_sec, monkeypatch):
        symbols = make_symbols([("GONE", "GONE", "0003333333", "current")])
        monkeypatch.setattr(reg, "_load_identity", lambda: (symbols, {}))
        result = reg.auto_register_index_only(["GONE"], csv_path=csv_path,
                                              today=TODAY)
        assert result["registered"] == []
        assert "404" in result["failed"][0][1]

    def test_companyfacts_error_blocked(self, csv_path, monkeypatch):
        """companyfacts 协调/传输失败不等于主体不存在：按未通过处理。"""
        symbols = make_symbols([("FLAKY", "FLAKY", "0006666666", "current")])
        monkeypatch.setattr(reg, "_load_identity", lambda: (symbols, {}))

        def boom(url, **kw):
            raise sec_client.SECCoordinationError("gate unavailable")

        monkeypatch.setattr(sec_client, "get", boom)
        result = reg.auto_register_index_only(["FLAKY"], csv_path=csv_path,
                                              today=TODAY)
        assert result["registered"] == []
        assert "探测失败" in result["failed"][0][1]
        assert not csv_path.exists() or read_csv(csv_path) == []


class TestRequestCap:
    def test_round_cap_50(self, csv_path, monkeypatch):
        symbols = make_symbols([
            (f"T{i:02d}", f"T{i:02d}", f"0001{i:06d}"[-10:], "current")
            for i in range(60)
        ])
        monkeypatch.setattr(reg, "_load_identity", lambda: (symbols, {}))
        calls = []

        class Resp:
            status_code = 200

        monkeypatch.setattr(sec_client, "get",
                            lambda url, **kw: calls.append(url) or Resp())
        result = reg.auto_register_index_only(
            [f"T{i:02d}" for i in range(60)], csv_path=csv_path, today=TODAY)
        assert len(calls) == 50               # 每轮上限 50
        assert len(result["registered"]) == 50
        assert len(result["failed"]) == 10
        assert all("上限" in reason for _, reason in result["failed"])


class TestSupplementAutoRegister:
    def test_auto_rows(self, supplement_csv):
        registered = reg.auto_register_supplement(
            ["ACME", "BETA"], csv_path=supplement_csv, today=TODAY)
        assert registered == ["ACME", "BETA"]
        rows = read_csv(supplement_csv)
        assert [r["registered_by"] for r in rows] == ["auto", "auto"]
        assert all("自动登记" in r["evidence_ref"] for r in rows)
        assert rows[0]["review_by"] == (TODAY + timedelta(days=90)).isoformat()

    def test_idempotent(self, supplement_csv):
        reg.auto_register_supplement(["ACME"], csv_path=supplement_csv,
                                     today=TODAY)
        reg.auto_register_supplement(["ACME"], csv_path=supplement_csv,
                                     today=TODAY)
        assert len(read_csv(supplement_csv)) == 1


class TestCsvCompat:
    def test_legacy_four_column_backfilled_manual(self, csv_path,
                                                  mock_identity, mock_sec):
        # 旧四列格式（无 registered_by）
        csv_path.write_text(
            "stock_code,evidence_ref,first_confirmed,review_by\n"
            'OLD1,"人工登记",2026-08-11,2026-11-11\n')
        reg.auto_register_index_only(["GOOD1"], csv_path=csv_path, today=TODAY)
        rows = read_csv(csv_path)
        assert len(rows) == 2
        assert rows[0]["stock_code"] == "OLD1"
        assert rows[0]["registered_by"] == "manual"   # 存量回填
        assert rows[1]["registered_by"] == "auto"
        # 表头升级后读取原有列不受影响
        assert rows[0]["evidence_ref"] == "人工登记"

    def test_scheduler_loader_reads_new_format(self, csv_path):
        from core import scheduler
        csv_path.write_text(
            "stock_code,evidence_ref,first_confirmed,review_by,registered_by\n"
            'NEW1,"自动登记",2026-10-22,2027-01-20,auto\n')
        import core.scheduler as sched
        orig = sched._PHASE_C_INDEX_ONLY_CSV
        try:
            sched._PHASE_C_INDEX_ONLY_CSV = csv_path
            assert sched._load_index_only_registry() == {"NEW1"}
        finally:
            sched._PHASE_C_INDEX_ONLY_CSV = orig


class TestAutoVerifyEntry:
    def test_combined_entry(self, csv_path, supplement_csv, mock_identity,
                            mock_sec):
        result = reg.auto_verify_and_register(
            ["GOOD2", "XE"], ["ACME"],
            index_only_csv=csv_path, supplement_csv=supplement_csv,
            today=TODAY)
        assert result["index_only_registered"] == ["GOOD2"]
        assert result["index_only_failed"][0][0] == "XE"
        assert result["supplement_registered"] == ["ACME"]
