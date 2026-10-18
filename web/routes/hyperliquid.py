"""Hyperliquid 只读看板：公开读取，无写操作。"""
import logging

from fastapi import APIRouter, HTTPException

from web import ok
from web.services import hyperliquid_service as service

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/hyperliquid/dashboard")
def hyperliquid_dashboard():
    try:
        return ok(service.get_dashboard())
    except service.HyperliquidUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception:
        logger.exception("hyperliquid dashboard failed")
        raise HTTPException(503, "Hyperliquid 看板服务不可用")
