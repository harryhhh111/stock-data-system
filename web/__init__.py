"""FastAPI JSON API for stock data dashboard."""

from typing import Any


def ok(data: Any = None) -> dict:
    """统一成功响应 {"ok": true, "data": ...}"""
    return {"ok": True, "data": data}


def err(error: str, detail: str | None = None) -> dict:
    """统一错误响应 {"ok": false, "error": "...", "detail": "..."}"""
    result: dict = {"ok": False, "error": error}
    if detail:
        result["detail"] = detail
    return result
