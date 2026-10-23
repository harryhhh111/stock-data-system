"""core/announcements/llm_enhance.py — 入库时 LLM 增强（每日资讯 v1.1）。

合同：docs/WATCHLIST_DAILY_NEWS_V1_1_PLAN.md（2026-10-23 审核通过）。

- 每条新资讯入库时一次 mmx 调用（模式复用
  web/services/watchlist_service.py 的 _run_mmx：subprocess 调
  `mmx text chat --non-interactive --quiet`，60s 超时，JSON 抽取）。
  core 层不反向依赖 web 层，这里按同一模式实现独立 runner。
- 媒体条目一次调用产出 {title_zh, summary_zh, importance}；
  官方披露（SEC/IR）只产出 {importance}（用户 2026-10-23 确认）。
- 红线：失败/超时/JSON 非法/字段缺失 → 条目正常入库、字段留 NULL，
  不阻塞采集、不静默（warning 日志 + 失败计数进整轮摘要）。
  importance 只接受 high|normal 两档，其他值按缺失处理。
- 存量不回填；LLM 结果不代表事实已核实。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

MMX_BIN = (os.environ.get("STOCK_WATCHLIST_MMX_BIN", "").strip()
           or shutil.which("mmx")
           or str(Path.home() / ".nvm/versions/node/v22.22.2/bin/mmx"))
# CLI 1.0.28 默认仍是 Preview 版，显式固定正式版模型；可用环境变量覆盖
MMX_MODEL = os.environ.get("STOCK_WATCHLIST_MMX_MODEL", "MiniMax-M3.1-Flash")

IMPORTANCE_LEVELS = ("high", "normal")


class EnhanceError(RuntimeError):
    """LLM 增强失败；调用方吞掉并留 NULL（不阻塞入库）。"""


def _run_mmx(system: str, user: str) -> str:
    """mmx 调用（与 watchlist_service._run_mmx 同一模式）。"""
    if shutil.which(MMX_BIN) is None and not Path(MMX_BIN).exists():
        raise EnhanceError("mmx CLI 不可用")
    try:
        proc = subprocess.run(
            [str(MMX_BIN), "text", "chat", "--non-interactive", "--quiet",
             "--model", MMX_MODEL, "--system", system, "--message", user],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        # 超时/进程错误同属增强失败：条目正常入库、字段留 NULL
        raise EnhanceError(f"mmx 调用失败: {exc}") from exc
    if proc.returncode != 0 or not proc.stdout.strip():
        raise EnhanceError(f"mmx 调用失败: {proc.stderr.strip()[:200] or '空输出'}")
    return proc.stdout


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise EnhanceError("模型输出不含 JSON")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise EnhanceError("模型输出不是 JSON 对象")
    return data


_MEDIA_SYSTEM = (
    "你是财经资讯编辑，为关注该公司的投资者处理一条英文资讯。"
    "只输出 JSON：{\"title_zh\": \"...\", \"summary_zh\": \"...\", "
    "\"importance\": \"high|normal\"}，不要输出其他内容。"
    "规则：title_zh 是标题的中文直译，不发挥、不添加标题没有的事实；"
    "summary_zh 是 2~3 句中文摘要，只依据给出的标题与摘要素材；"
    "没有摘要素材时仅据标题概括，不编造事实、数字与结论；"
    "importance 判断该资讯对关注该公司的投资者的重要性："
    "high = 业绩/指引/重大合同/管理层变动/监管/评级重大变化/股价异动驱动事件；"
    "normal = 常规报道与弱相关提及。拿不准给 normal。"
)

_OFFICIAL_SYSTEM = (
    "你是财经资讯编辑，判断一条官方披露对关注该公司的投资者的重要性。"
    "只输出 JSON：{\"importance\": \"high|normal\"}，不要输出其他内容。"
    "规则：high = 8-K 重大事项/季报年报/管理层变动/重大合同与指引；"
    "normal = 常规申报与程序性披露。拿不准给 normal。"
)


def enhance_media(title: str, summary: str, stock_code: str) -> dict:
    """媒体条目：一次调用产出三字段。失败抛 EnhanceError。"""
    user = (f"公司代码：{stock_code}\n标题：{title}\n"
            f"摘要素材：{summary or '（无）'}")
    data = _extract_json(_run_mmx(_MEDIA_SYSTEM, user))
    title_zh = str(data.get("title_zh") or "").strip()
    summary_zh = str(data.get("summary_zh") or "").strip()
    importance = str(data.get("importance") or "").strip().lower()
    if importance not in IMPORTANCE_LEVELS:
        importance = ""
    if not title_zh or not summary_zh or not importance:
        raise EnhanceError(f"模型输出字段缺失: {data!r}")
    return {"title_zh": title_zh[:200], "summary_zh": summary_zh[:500],
            "importance": importance}


def enhance_official(title: str, form_type: str, stock_code: str) -> dict:
    """官方披露（SEC/IR）：只算 importance。失败抛 EnhanceError。"""
    user = f"公司代码：{stock_code}\n表单类型：{form_type}\n标题：{title}"
    data = _extract_json(_run_mmx(_OFFICIAL_SYSTEM, user))
    importance = str(data.get("importance") or "").strip().lower()
    if importance not in IMPORTANCE_LEVELS:
        raise EnhanceError(f"importance 非法: {data!r}")
    return {"importance": importance}
