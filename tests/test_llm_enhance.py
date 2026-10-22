"""LLM 增强（mmx）离线测试：全部 mock _run_mmx，不调用真实 mmx。

合同：docs/WATCHLIST_DAILY_NEWS_V1_1_PLAN.md §1/§4——JSON 抽取失败、
超时、空输出、importance 缺失/非法均不阻塞入库（字段留 NULL）。
"""
import json
import subprocess

import pytest

from core.announcements import llm_enhance


def mock_mmx(monkeypatch, *, stdout=None, raises=None):
    if raises is not None:
        def boom(*_a, **_kw):
            raise raises
        monkeypatch.setattr(llm_enhance, "_run_mmx", boom)
    else:
        monkeypatch.setattr(llm_enhance, "_run_mmx", lambda *a, **kw: stdout)


class TestMediaEnhance:
    def test_ok(self, monkeypatch):
        mock_mmx(monkeypatch, stdout=json.dumps({
            "title_zh": "美光上调回购授权", "summary_zh": "美光宣布……",
            "importance": "high"}))
        out = llm_enhance.enhance_media("Micron raises buyback", "", "MU")
        assert out == {"title_zh": "美光上调回购授权",
                       "summary_zh": "美光宣布……", "importance": "high"}

    def test_fenced_json(self, monkeypatch):
        mock_mmx(monkeypatch, stdout='```json\n{"title_zh": "标题", '
                                     '"summary_zh": "摘要", '
                                     '"importance": "normal"}\n```')
        out = llm_enhance.enhance_media("t", "s", "MU")
        assert out["importance"] == "normal"

    def test_invalid_json(self, monkeypatch):
        mock_mmx(monkeypatch, stdout="这不是 JSON")
        with pytest.raises(llm_enhance.EnhanceError):
            llm_enhance.enhance_media("t", "", "MU")

    def test_empty_output(self, monkeypatch):
        mock_mmx(monkeypatch, raises=llm_enhance.EnhanceError("空输出"))
        with pytest.raises(llm_enhance.EnhanceError):
            llm_enhance.enhance_media("t", "", "MU")

    def test_timeout(self, monkeypatch):
        # 超时与进程错误统一包装为 EnhanceError（不穿透采集路径）
        mock_mmx(monkeypatch, raises=llm_enhance.EnhanceError("mmx 调用失败: 超时"))
        with pytest.raises(llm_enhance.EnhanceError):
            llm_enhance.enhance_media("t", "", "MU")

    def test_missing_importance(self, monkeypatch):
        mock_mmx(monkeypatch, stdout=json.dumps({
            "title_zh": "标题", "summary_zh": "摘要"}))
        with pytest.raises(llm_enhance.EnhanceError, match="字段缺失"):
            llm_enhance.enhance_media("t", "", "MU")

    def test_invalid_importance_value(self, monkeypatch):
        mock_mmx(monkeypatch, stdout=json.dumps({
            "title_zh": "标题", "summary_zh": "摘要", "importance": "urgent"}))
        with pytest.raises(llm_enhance.EnhanceError, match="字段缺失"):
            llm_enhance.enhance_media("t", "", "MU")


class TestOfficialEnhance:
    def test_importance_only(self, monkeypatch):
        """官方披露只算 importance，不出中文标题（用户确认口径）。"""
        captured = {}

        def fake(system, user):
            captured["system"] = system
            return '{"importance": "high"}'

        monkeypatch.setattr(llm_enhance, "_run_mmx", fake)
        out = llm_enhance.enhance_official("8-K 当前报告", "8-K", "MU")
        assert out == {"importance": "high"}
        assert "title_zh" not in out

    def test_invalid_importance_rejected(self, monkeypatch):
        mock_mmx(monkeypatch, stdout='{"importance": "critical"}')
        with pytest.raises(llm_enhance.EnhanceError):
            llm_enhance.enhance_official("8-K", "8-K", "MU")


class TestRunner:
    def test_subprocess_timeout_wrapped(self, monkeypatch):
        def boom(*_a, **_kw):
            raise subprocess.TimeoutExpired("mmx", 60)
        monkeypatch.setattr(llm_enhance.subprocess, "run", boom)
        monkeypatch.setattr(llm_enhance.shutil, "which", lambda _x: "/bin/true")
        with pytest.raises(llm_enhance.EnhanceError):
            llm_enhance._run_mmx("s", "u")

    def test_nonzero_exit_and_empty_output(self, monkeypatch):
        class Proc:
            returncode = 1
            stdout = ""
            stderr = "boom"
        monkeypatch.setattr(llm_enhance.subprocess, "run",
                            lambda *a, **kw: Proc())
        monkeypatch.setattr(llm_enhance.shutil, "which", lambda _x: "/bin/true")
        with pytest.raises(llm_enhance.EnhanceError):
            llm_enhance._run_mmx("s", "u")
