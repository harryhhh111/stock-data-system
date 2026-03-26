"""
数据库管理模块
"""
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Generator, List, Optional, Dict, Any
from sqlalchemy.orm import Session
import models
from models import get_session, get_engine, init_db

logger = logging.getLogger(__name__)


@contextmanager
def get_db_session() -> Generator[Session, None, None]:
    """上下文管理器：自动管理数据库会话"""
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"Database error: {e}")
        raise
    finally:
        session.close()


def get_stock_codes(market: Optional[str] = None) -> List[str]:
    """获取股票代码列表"""
    with get_db_session() as session:
        query = session.query(models.StockInfo.stock_code)
        if market:
            query = query.filter(models.StockInfo.market == market)
        return [row[0] for row in query.all()]


def stock_exists(stock_code: str) -> bool:
    """检查股票是否存在"""
    with get_db_session() as session:
        return session.query(
            session.query(models.StockInfo).filter(
                models.StockInfo.stock_code == stock_code
            ).exists()
        ).scalar()


def get_latest_indicator_date(stock_code: str) -> Optional[str]:
    """获取某股票最新指标日期"""
    with get_db_session() as session:
        result = session.query(models.FinancialIndicator.indicator_date).filter(
            models.FinancialIndicator.stock_code == stock_code
        ).order_by(
            models.FinancialIndicator.indicator_date.desc()
        ).first()
        return result[0].isoformat() if result else None


def get_latest_report_date(stock_code: str, table_name: str) -> Optional[str]:
    """获取某股票最新报表日期"""
    with get_db_session() as session:
        table = getattr(models, table_name)
        result = session.query(table.report_date).filter(
            table.stock_code == stock_code
        ).order_by(
            table.report_date.desc()
        ).first()
        return result[0] if result else None


def get_table_count(table_name: str) -> int:
    """获取表记录数"""
    with get_db_session() as session:
        table = getattr(models, table_name)
        return session.query(table).count()


def init_database():
    """初始化数据库"""
    logger.info(f"Initializing database: {models.get_engine().url}")
    init_db()
    logger.info("Database initialized successfully")


# ============================================================
# 同步日志辅助函数
# ============================================================

def get_latest_sync_log() -> Optional[Dict[str, Any]]:
    """获取最近一条同步日志"""
    with get_db_session() as session:
        log = session.query(models.SyncLog).order_by(
            models.SyncLog.start_time.desc()
        ).first()
        if log:
            return {
                "sync_type": log.sync_type,
                "status": log.status,
                "start_time": log.start_time,
                "end_time": log.end_time,
                "records_synced": log.records_synced,
                "records_failed": log.records_failed,
                "error_message": log.error_message,
            }
        return None


def get_sync_logs(limit: int = 20) -> List[Dict[str, Any]]:
    """获取最近的同步日志"""
    with get_db_session() as session:
        logs = session.query(models.SyncLog).order_by(
            models.SyncLog.start_time.desc()
        ).limit(limit).all()
        return [
            {
                "sync_type": log.sync_type,
                "status": log.status,
                "start_time": log.start_time,
                "end_time": log.end_time,
                "records_synced": log.records_synced,
                "records_failed": log.records_failed,
                "error_message": log.error_message,
            }
            for log in logs
        ]


def get_sync_stats_by_type() -> Dict[str, Dict[str, Any]]:
    """按类型获取同步统计"""
    from sqlalchemy import func
    with get_db_session() as session:
        results = session.query(
            models.SyncLog.sync_type,
            func.count(models.SyncLog.id).label("total"),
            func.sum(
                models.SyncLog.records_synced
            ).label("total_records"),
            func.max(models.SyncLog.start_time).label("last_sync"),
        ).group_by(models.SyncLog.sync_type).all()

        return {
            row.sync_type: {
                "total_runs": row.total,
                "total_records": int(row.total_records or 0),
                "last_sync": row.last_sync,
            }
            for row in results
        }
