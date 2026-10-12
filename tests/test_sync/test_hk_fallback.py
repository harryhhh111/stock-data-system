"""
tests/test_sync/test_hk_fallback.py — 港股 capex NULL 自动兜底（core/sync/hk_fallback.py）单元测试

不触网、不连库：DB 用 fake Connection，T1/T2 脚本经 sys.modules 注入假模块，
队列/audit 用 tmp_path 隔离。
"""
import json
import os
import sys
import types
from datetime import date
from pathlib import Path

from core.sync import hk_fallback


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
        encoding="utf-8",
    )


# ── _load_skip_keys ────────────────────────────────────────


class TestLoadSkipKeys:
    def test_last_status_wins(self, tmp_path):
        audit = tmp_path / "audit.jsonl"
        _write_jsonl(audit, [
            # 后来变成 no_capex_row → 跳过
            {"stock_code": "00001", "report_date": "2026-06-30", "report_type": "semi", "status": "failed"},
            {"stock_code": "00001", "report_date": "2026-06-30", "report_type": "semi", "status": "no_capex_row"},
            # 人工修复后重试成功 → 不再跳过
            {"stock_code": "00002", "report_date": "2026-06-30", "report_type": "semi", "status": "no_capex_row"},
            {"stock_code": "00002", "report_date": "2026-06-30", "report_type": "semi", "status": "ok"},
        ])
        keys = hk_fallback._load_skip_keys(audit)
        assert keys == {("00001", "2026-06-30", "semi")}

    def test_missing_file(self, tmp_path):
        assert hk_fallback._load_skip_keys(tmp_path / "nope.jsonl") == set()

    def test_bad_lines_ignored(self, tmp_path):
        audit = tmp_path / "audit.jsonl"
        audit.write_text(
            "not json\n"
            '{"stock_code":"00003","report_date":"2026-06-30","report_type":"semi","status":"unit_unknown"}\n',
            encoding="utf-8",
        )
        assert ("00003", "2026-06-30", "semi") in hk_fallback._load_skip_keys(audit)


# ── append_to_queue ────────────────────────────────────────


class TestAppendToQueue:
    def test_dedup_skip_and_serialize(self, tmp_path):
        queue = tmp_path / "q.jsonl"
        audit = tmp_path / "a.jsonl"
        _write_jsonl(queue, [
            {"stock_code": "00010", "stock_name": "X", "report_date": "2026-06-30", "report_type": "semi"},
        ])
        _write_jsonl(audit, [
            {"stock_code": "00020", "report_date": "2025-12-31", "report_type": "annual", "status": "no_capex_row"},
        ])
        candidates = [
            # 已在队列 → already
            {"stock_code": "00010", "stock_name": "X", "report_date": date(2026, 6, 30), "report_type": "semi"},
            # 历史无解 → skipped_prior
            {"stock_code": "00020", "stock_name": "Y", "report_date": date(2025, 12, 31), "report_type": "annual"},
            # 新候选（date 与 str 同 key 重复）→ 只入队一次
            {"stock_code": "00030", "stock_name": "Z", "report_date": date(2026, 6, 30), "report_type": "semi"},
            {"stock_code": "00030", "stock_name": "Z", "report_date": "2026-06-30", "report_type": "semi"},
            # failed 状态可重试 → 入队
            {"stock_code": "00040", "stock_name": "W", "report_date": date(2026, 6, 30), "report_type": "semi"},
        ]
        stats = hk_fallback.append_to_queue(candidates, path=queue, audit_path=audit)
        assert stats == {"queued": 2, "already_queued": 2, "skipped_prior": 1}

        entries = hk_fallback.load_queue(queue)
        assert len(entries) == 3
        assert {e["stock_code"] for e in entries} == {"00010", "00030", "00040"}
        new = next(e for e in entries if e["stock_code"] == "00030")
        assert new["report_date"] == "2026-06-30"  # date 序列化为字符串
        assert new["queued_by"] == "hk_fallback"


# ── drain_pdf_queue ────────────────────────────────────────


class TestDrainPdfQueue:
    @staticmethod
    def _fake_t2_module(monkeypatch, captured: list):
        mod = types.ModuleType("scripts.extract_hk_capex_pdf")

        def fake_run(targets, dry_run):
            captured.extend(targets)
            return {
                "stocks_total": len(targets), "written": 1,
                "review_queue": 0, "escalate_exhausted": len(targets) - 1, "failed": 0,
            }

        mod.run = fake_run
        monkeypatch.setitem(sys.modules, "scripts.extract_hk_capex_pdf", mod)

    def test_drain_takes_limit_and_persists_rest(self, tmp_path, monkeypatch):
        queue = tmp_path / "q.jsonl"
        _write_jsonl(queue, [
            {
                "stock_code": f"{i:05d}", "stock_name": None, "report_date": "2026-06-30",
                "report_type": "semi", "queued_at": "x", "queued_by": "hk_fallback",
            }
            for i in range(5)
        ])
        monkeypatch.setattr(hk_fallback, "QUEUE_PATH", queue)
        captured: list = []
        self._fake_t2_module(monkeypatch, captured)

        result = hk_fallback.drain_pdf_queue(limit=2, dry_run=False)

        assert result["queue_before"] == 5
        assert result["processed"] == 2
        assert result["remaining"] == 3
        assert result["written"] == 1
        assert len(captured) == 2
        # report_date 须转回 date 对象（T2 的期间校验依赖 date 运算）
        assert all(isinstance(t["report_date"], date) for t in captured)
        rest = hk_fallback.load_queue(queue)
        assert len(rest) == 3
        assert {e["stock_code"] for e in rest} == {"00002", "00003", "00004"}

    def test_empty_queue_noop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hk_fallback, "QUEUE_PATH", tmp_path / "q.jsonl")
        result = hk_fallback.drain_pdf_queue(limit=80, dry_run=False)
        assert result["queue_before"] == 0
        assert result["processed"] == 0

    def test_dry_run_does_not_call_t2(self, tmp_path, monkeypatch):
        queue = tmp_path / "q.jsonl"
        _write_jsonl(queue, [
            {"stock_code": "00001", "report_date": "2026-06-30", "report_type": "semi"}
        ])
        monkeypatch.setattr(hk_fallback, "QUEUE_PATH", queue)
        captured: list = []
        self._fake_t2_module(monkeypatch, captured)

        result = hk_fallback.drain_pdf_queue(limit=80, dry_run=True)

        assert captured == []
        assert result["processed"] == 1
        assert result["remaining"] == 0  # 仅模拟
        assert len(hk_fallback.load_queue(queue)) == 1  # 文件不动


# ── detect_null_candidates ─────────────────────────────────


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql):
        pass

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeCursor(self._rows)


class TestDetect:
    def test_entries_shape(self, monkeypatch):
        rows = [("00001", "腾讯控股", date(2026, 6, 30), "semi")]
        monkeypatch.setattr(hk_fallback, "Connection", lambda: _FakeConn(rows))
        entries = hk_fallback.detect_null_candidates()
        assert entries == [{
            "stock_code": "00001", "stock_name": "腾讯控股",
            "report_date": date(2026, 6, 30), "report_type": "semi",
        }]


# ── run_hk_capex_fallback ──────────────────────────────────


class TestRunFallback:
    @staticmethod
    def _fake_t1_module(monkeypatch, calls: list):
        mod = types.ModuleType("scripts.backfill_hk_capex_tencent")

        def fake_run(codes, dry_run):
            calls.append((list(codes), dry_run))
            return {"stocks_total": len(codes), "written": 3}

        mod.run = fake_run
        monkeypatch.setitem(sys.modules, "scripts.backfill_hk_capex_tencent", mod)

    def test_dry_run_skips_t1_and_disk(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hk_fallback, "QUEUE_PATH", tmp_path / "q.jsonl")
        monkeypatch.setattr(hk_fallback, "T2_AUDIT_PATH", tmp_path / "a.jsonl")
        candidates = [
            {"stock_code": "00001", "stock_name": "X", "report_date": date(2026, 6, 30), "report_type": "semi"},
            {"stock_code": "00002", "stock_name": "Y", "report_date": date(2025, 12, 31), "report_type": "annual"},
        ]
        monkeypatch.setattr(hk_fallback, "detect_null_candidates", lambda: list(candidates))
        t1_calls: list = []
        self._fake_t1_module(monkeypatch, t1_calls)

        summary = hk_fallback.run_hk_capex_fallback(dry_run=True)

        assert summary["candidates"] == 2
        assert summary["candidate_stocks"] == 2
        assert summary["queued"] == 2  # 仅模拟入队
        assert t1_calls == []  # dry-run 不调 T1
        assert not (tmp_path / "q.jsonl").exists()  # 不写队列文件

    def test_full_run_t1_then_queue(self, tmp_path, monkeypatch):
        queue = tmp_path / "q.jsonl"
        audit = tmp_path / "a.jsonl"
        monkeypatch.setattr(hk_fallback, "QUEUE_PATH", queue)
        monkeypatch.setattr(hk_fallback, "T2_AUDIT_PATH", audit)
        _write_jsonl(audit, [
            {"stock_code": "00002", "report_date": "2025-12-31", "report_type": "annual", "status": "no_capex_row"},
        ])
        first = [
            {"stock_code": "00001", "stock_name": "X", "report_date": date(2026, 6, 30), "report_type": "semi"},
            {"stock_code": "00002", "stock_name": "Y", "report_date": date(2025, 12, 31), "report_type": "annual"},
        ]
        remaining = [first[1]]  # T1 补上了 00001，复查只剩 00002
        seq = iter([first, remaining])
        monkeypatch.setattr(hk_fallback, "detect_null_candidates", lambda: next(seq))
        t1_calls: list = []
        self._fake_t1_module(monkeypatch, t1_calls)

        summary = hk_fallback.run_hk_capex_fallback(dry_run=False)

        assert t1_calls == [(["00001", "00002"], False)]
        assert summary["t1_written"] == 3
        assert summary["queued"] == 0  # 00002 命中 skip
        assert summary["skipped_prior"] == 1
        assert not queue.exists()  # 无人入队时不创建队列文件


# ── config 字段 ────────────────────────────────────────────

_ENV_VARS = (
    "STOCK_HK_CAPEX_FALLBACK_AUTO",
    "STOCK_HK_CAPEX_PDF_CRON",
    "STOCK_HK_CAPEX_PDF_NIGHTLY_LIMIT",
)


class TestSchedulerConfigHkFallback:
    def test_defaults(self):
        for var in _ENV_VARS:
            os.environ.pop(var, None)
        from config import SchedulerConfig

        cfg = SchedulerConfig()
        assert cfg.hk_capex_fallback_auto is True
        assert cfg.hk_capex_pdf_cron == "47 23 * * *"
        assert cfg.hk_capex_pdf_nightly_limit == 80

    def test_env_override(self):
        os.environ["STOCK_HK_CAPEX_FALLBACK_AUTO"] = "0"
        os.environ["STOCK_HK_CAPEX_PDF_CRON"] = "30 1 * * *"
        os.environ["STOCK_HK_CAPEX_PDF_NIGHTLY_LIMIT"] = "40"
        try:
            from config import SchedulerConfig

            cfg = SchedulerConfig()
            assert cfg.hk_capex_fallback_auto is False
            assert cfg.hk_capex_pdf_cron == "30 1 * * *"
            assert cfg.hk_capex_pdf_nightly_limit == 40
        finally:
            for var in _ENV_VARS:
                os.environ.pop(var, None)
