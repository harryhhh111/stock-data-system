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
    """默认身份源：策划表 GOOD1→CIK 0001111111，GOOD2→CIK 0002222222；
    SEC 全量映射 FULL1→0008888881、TAKEN→0008888882（TAKEN 的 CIK 已被
    stock_info 中 OTHER 占用）。"""
    symbols = make_symbols([
        ("GOOD1", "GOOD1", "0001111111", "current"),
        ("GOOD2", "GOOD2", "0002222222", "current"),
        ("CONFLICT", "AAA", "0003333333", "current"),
        ("CONFLICT", "BBB", "0004444444", "legacy"),
        ("MISMATCH", "MISMATCH", "0005555555", "current"),
    ])
    stock_cik = {"MISMATCH": "0009999999",  # 与身份行不一致
                 "OTHER": "0008888882"}     # 占用 TAKEN 的 CIK
    sec_map = {"FULL1": "0008888881", "TAKEN": "0008888882",
               "GOOD1": "0007777771"}      # GOOD1 策划表优先，不用此值
    monkeypatch.setattr(reg, "_load_identity",
                        lambda: (symbols, stock_cik, sec_map))
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
        assert result["registered"] == ["GOOD1"]
        assert result["failed"] == []
        assert result["failed_by_reason"] == {}
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
        assert "SEC 全量映射无此 ticker" in result["failed"][0][1]
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
        monkeypatch.setattr(reg, "_load_identity", lambda: (symbols, {}, {}))
        result = reg.auto_register_index_only(["GONE"], csv_path=csv_path,
                                              today=TODAY)
        assert result["registered"] == []
        assert "404" in result["failed"][0][1]

    def test_companyfacts_error_blocked(self, csv_path, monkeypatch):
        """companyfacts 协调/传输失败不等于主体不存在：按未通过处理。"""
        symbols = make_symbols([("FLAKY", "FLAKY", "0006666666", "current")])
        monkeypatch.setattr(reg, "_load_identity", lambda: (symbols, {}, {}))

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
        monkeypatch.setattr(reg, "_load_identity", lambda: (
            symbols, {}, {f"T{i:02d}": f"0001{i:06d}"[-10:]
                          for i in range(60)}))
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


class TestR2IdentityResolution:
    """R2：双身份源解析顺序与 fail-closed（方案 §测试）。"""

    def test_curated_table_wins_over_full_map(self, csv_path, mock_identity,
                                              monkeypatch):
        """策划表命中优先于全量映射：GOOD1 在全量映射里指向别的 CIK，
        登记证据必须用策划表 CIK。"""
        class Resp:
            status_code = 200
        monkeypatch.setattr(sec_client, "get", lambda url, **kw: Resp())
        result = reg.auto_register_index_only(["GOOD1"], csv_path=csv_path,
                                              today=TODAY)
        assert result["registered"] == ["GOOD1"]
        row = read_csv(csv_path)[0]
        assert "CIK0001111111" in row["evidence_ref"]
        assert "CIK0007777771" not in row["evidence_ref"]

    def test_full_map_hit_passes(self, csv_path, mock_identity, monkeypatch):
        """策划表无映射 → SEC 全量映射命中 → companyfacts 200 → 通过。"""
        class Resp:
            status_code = 200
        monkeypatch.setattr(sec_client, "get", lambda url, **kw: Resp())
        result = reg.auto_register_index_only(["FULL1"], csv_path=csv_path,
                                              today=TODAY)
        assert result["registered"] == ["FULL1"]
        row = read_csv(csv_path)[0]
        assert "CIK0008888881" in row["evidence_ref"]
        assert "不声明财务解析可用" in row["evidence_ref"]

    def test_cik_occupied_by_other_code_conflicts(self, csv_path,
                                                  mock_identity, monkeypatch):
        """全量映射 CIK 已被 stock_info 其他代码占用 → 冲突失败写明双方。"""
        class Resp:
            status_code = 200
        monkeypatch.setattr(sec_client, "get", lambda url, **kw: Resp())
        result = reg.auto_register_index_only(["TAKEN"], csv_path=csv_path,
                                              today=TODAY)
        assert result["registered"] == []
        reason = result["failed"][0][1]
        assert "身份冲突" in reason and "OTHER" in reason and "TAKEN" in reason
        assert not csv_path.exists() or read_csv(csv_path) == []

    def test_full_map_miss_blocked(self, csv_path, mock_identity):
        """两个身份源都无映射 → 阻塞，原因写明，不发 SEC 请求。"""
        result = reg.auto_register_index_only(["XE"], csv_path=csv_path,
                                              today=TODAY)
        assert result["registered"] == []
        assert "SEC 全量映射无此 ticker" in result["failed"][0][1]

    def test_missing_cache_fail_closed(self, csv_path, monkeypatch):
        """company_tickers.json 缓存缺失 → 本轮全部保持阻塞，零 SEC 请求。"""
        monkeypatch.setattr(reg, "_load_identity", lambda: ([], {}, None))
        calls = []
        monkeypatch.setattr(sec_client, "get",
                            lambda url, **kw: calls.append(url))
        result = reg.auto_register_index_only(["FULL1", "XE"],
                                              csv_path=csv_path, today=TODAY)
        assert result["registered"] == []
        assert len(result["failed"]) == 2
        assert all("缓存缺失" in r for _, r in result["failed"])
        assert result["failed_by_reason"] == {"身份映射缓存缺失": 2}
        assert calls == []
        assert not csv_path.exists() or read_csv(csv_path) == []


# 2026-10-23 运行的 41 只 index-only 阻塞清单（方案背景）
BLOCKED_41 = ("DY ENSG FIG FN FORM FPS GFL GH GLXY GSAT HL IESC IONQ IREN "
              "JBS JMKE KTOS KVYO MBGL MDGL MFP MOD NXT OKLO PL POWL PRIM "
              "RMBS SANM SITM SMTC SPXC STRL SUNB TTMI ULS VIAV VICR VMRK "
              "WTS WULF").split()
INDEX_ONLY_FIXTURE = (
    Path(__file__).parent / "fixtures" / "sec" / "company_tickers_index_only.json"
)


class TestBlocked41Replay:
    """离线重放 41 只（fixture 为真实 company_tickers.json 的相关子集）。"""

    @pytest.fixture
    def replay_identity(self, monkeypatch):
        sec_map = reg.load_sec_ticker_map(INDEX_ONLY_FIXTURE)
        assert sec_map is not None and len(sec_map) == 41
        # 策划表为空、stock_info 为空（index-only 不在 universe）
        monkeypatch.setattr(reg, "_load_identity",
                            lambda: ([], {}, sec_map))

    def test_all_41_identity_verified(self, csv_path, replay_identity,
                                      monkeypatch):
        """全部 41 只经全量映射解析出 CIK；companyfacts 200 → 全部登记。"""
        class Resp:
            status_code = 200
        monkeypatch.setattr(sec_client, "get", lambda url, **kw: Resp())
        result = reg.auto_register_index_only(BLOCKED_41, csv_path=csv_path,
                                              today=TODAY)
        assert sorted(result["registered"]) == sorted(BLOCKED_41)
        assert result["failed"] == []
        assert len(read_csv(csv_path)) == 41

    def test_companyfacts_failures_keep_blocking_with_reason(
            self, csv_path, replay_identity, monkeypatch):
        """companyfacts 非 200 者维持阻塞并带具体原因（GFL/JBS 形态：
        身份可登记但探测/解析失败不豁免）。"""
        class Resp:
            def __init__(self, status):
                self.status_code = status
        def fake_get(url, **kw):
            cik = url.split("CIK")[1].split(".")[0]
            # GFL/JBS 探测 404：不解除阻塞
            fail_ciks = {reg.load_sec_ticker_map(INDEX_ONLY_FIXTURE)[t]
                         for t in ("GFL", "JBS")}
            return Resp(404 if cik in fail_ciks else 200)
        monkeypatch.setattr(sec_client, "get", fake_get)
        result = reg.auto_register_index_only(BLOCKED_41, csv_path=csv_path,
                                              today=TODAY)
        assert len(result["registered"]) == 39
        assert sorted(t for t, _ in result["failed"]) == ["GFL", "JBS"]
        assert all("404" in r for _, r in result["failed"])
        assert result["failed_by_reason"] == {"companyfacts HTTP 404": 2}
        assert len(read_csv(csv_path)) == 39
