"""Hyperliquid 只读看板：公开读取，无写操作。"""
import logging

from fastapi import APIRouter, HTTPException

from web import ok
from web.services import hyperliquid_pairs_service as pairs_service
from web.services import hyperliquid_service as service

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/hyperliquid/pairs")
def hyperliquid_pairs():
    try:
        revision, pairs = pairs_service.load_pairs()
        return ok({
            "schema_version": pairs_service.SCHEMA_VERSION,
            "config_revision": revision,
            "pairs": pairs,
        })
    except pairs_service.PairsConfigError as exc:
        if exc.public:
            raise HTTPException(503, exc.public) from exc
        logger.warning("hyperliquid pairs config invalid: %s", exc)
        raise HTTPException(503, "Hyperliquid 多 pair 配置不可用") from exc


@router.get("/hyperliquid/pairs/{pair_id}/dashboard")
def pair_dashboard(pair_id: str):
    try:
        return ok(pairs_service.get_pair_dashboard(pair_id))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except pairs_service.PairsConfigError as exc:
        if exc.public:
            raise HTTPException(503, exc.public) from exc
        logger.warning("hyperliquid pairs config invalid: %s", exc)
        raise HTTPException(503, "Hyperliquid 多 pair 配置不可用") from exc
    except service.HyperliquidUnavailable as exc:
        if exc.public:
            raise HTTPException(503, exc.public) from exc
        logger.warning("hyperliquid pair dashboard unavailable: %s", exc)
        raise HTTPException(503, "Hyperliquid pair 看板暂不可用") from exc
    except Exception:
        logger.exception("hyperliquid pair dashboard failed")
        raise HTTPException(503, "Hyperliquid pair 看板暂不可用")


@router.get("/hyperliquid/announcements")
def hyperliquid_announcements():
    try:
        return ok(pairs_service.get_announcements())
    except pairs_service.PairsConfigError as exc:
        if exc.public:
            raise HTTPException(503, exc.public) from exc
        logger.warning("hyperliquid pairs config invalid: %s", exc)
        raise HTTPException(503, "Hyperliquid 多 pair 配置不可用") from exc
    except service.HyperliquidUnavailable as exc:
        if exc.public:
            raise HTTPException(503, exc.public) from exc
        logger.warning("hyperliquid announcements unavailable: %s", exc)
        raise HTTPException(503, "Hyperliquid 公告暂不可用") from exc
    except Exception:
        logger.exception("hyperliquid announcements failed")
        raise HTTPException(503, "Hyperliquid 公告暂不可用")


@router.get("/hyperliquid/dashboard")
def hyperliquid_dashboard():
    try:
        return ok(service.get_dashboard())
    except service.HyperliquidUnavailable as exc:
        if exc.public:
            raise HTTPException(503, exc.public) from exc
        logger.warning("hyperliquid dashboard unavailable: %s", exc)
        raise HTTPException(503, "Hyperliquid 看板服务暂不可用") from exc
    except Exception:
        logger.exception("hyperliquid dashboard failed")
        raise HTTPException(503, "Hyperliquid 看板服务暂不可用")
