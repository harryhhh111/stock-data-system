"""
FastAPI 查询服务
提供RESTful API查询股票基本面数据
"""
import logging
from typing import List, Optional
from datetime import datetime, date
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import func, desc

import config
import models
from models import (
    get_session, get_engine,
    StockInfo, FinancialIndicator, IncomeStatement,
    BalanceSheet, CashFlowStatement, IndexConstituent, SyncLog,
    init_db
)

logger = logging.getLogger(__name__)

# ============================================================
# Pydantic 响应模型
# ============================================================

class StockInfoOut(BaseModel):
    stock_code: str
    stock_name: str
    market: str
    exchange: Optional[str] = None
    industry: Optional[str] = None
    list_date: Optional[date] = None

    class Config:
        from_attributes = True


class FinancialIndicatorOut(BaseModel):
    stock_code: str
    indicator_date: date
    pe: Optional[float] = None
    pb: Optional[float] = None
    roe: Optional[float] = None
    gross_margin: Optional[float] = None
    net_margin: Optional[float] = None
    revenue_growth: Optional[float] = None
    profit_growth: Optional[float] = None

    class Config:
        from_attributes = True


class IncomeStatementOut(BaseModel):
    stock_code: str
    report_date: str
    revenue: Optional[float] = None
    net_profit: Optional[float] = None
    attr_profit: Optional[float] = None
    eps: Optional[float] = None

    class Config:
        from_attributes = True


class IndexConstituentOut(BaseModel):
    index_code: str
    index_name: Optional[str] = None
    stock_code: str
    stock_name: Optional[str] = None

    class Config:
        from_attributes = True


class DashboardStats(BaseModel):
    total_stocks: int
    cn_a_stocks: int
    hk_stocks: int
    financial_indicators: int
    income_statements: int
    balance_sheets: int
    cash_flow_statements: int
    index_constituents: int


class HealthResponse(BaseModel):
    status: str                            # "healthy" / "degraded" / "unhealthy"
    service: str = "stock_data_api"
    version: str = "1.0.0"
    db_connected: bool
    last_sync_time: Optional[datetime] = None
    last_sync_status: Optional[str] = None
    circuit_breaker_open: bool = False


class SyncStatusItem(BaseModel):
    sync_type: str
    status: str
    start_time: datetime
    end_time: Optional[datetime] = None
    records_synced: int = 0
    records_failed: int = 0
    error_message: Optional[str] = None

    class Config:
        from_attributes = True


class SyncStatusResponse(BaseModel):
    recent_syncs: List[SyncStatusItem]
    total_syncs: int


# ============================================================
# FastAPI 应用
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动和关闭时的生命周期管理"""
    # 启动时
    logger.info("Initializing database...")
    init_db()
    logger.info("Database initialized")
    yield
    # 关闭时
    logger.info("Shutting down...")


app = FastAPI(
    title="股票基本面数据API",
    description="A股/港股基本面数据查询服务",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# 路由
# ============================================================

@app.get("/", tags=["健康检查"])
async def root():
    """健康检查（简单版）"""
    return {"status": "ok", "service": "stock_data_api", "version": "1.0.0"}


@app.get("/health", response_model=HealthResponse, tags=["健康检查"])
async def health_check():
    """
    健康检查（详细版）
    - 检查数据库连接状态
    - 返回最近一次同步的时间和状态
    - 返回熔断器状态
    """
    db_connected = False
    last_sync_time = None
    last_sync_status = None
    circuit_breaker_open = False

    # 检查数据库连接
    try:
        with get_session() as session:
            count = session.query(StockInfo).count()
            db_connected = True

            # 查询最近一次同步记录
            latest_sync = session.query(SyncLog).order_by(
                SyncLog.start_time.desc()
            ).first()
            if latest_sync:
                last_sync_time = latest_sync.start_time
                last_sync_status = latest_sync.status
    except Exception as e:
        logger.error(f"Health check - DB connection failed: {e}")

    # 检查熔断器状态
    try:
        from data_fetcher import circuit_breaker
        circuit_breaker_open = circuit_breaker.is_open()
    except Exception:
        pass

    # 综合判断
    if not db_connected:
        status = "unhealthy"
    elif circuit_breaker_open:
        status = "degraded"
    else:
        status = "healthy"

    return HealthResponse(
        status=status,
        db_connected=db_connected,
        last_sync_time=last_sync_time,
        last_sync_status=last_sync_status,
        circuit_breaker_open=circuit_breaker_open,
    )


@app.get("/sync/status", response_model=SyncStatusResponse, tags=["同步状态"])
async def get_sync_status(
    limit: int = Query(20, ge=1, le=100, description="返回最近N条同步记录"),
):
    """查询数据同步状态"""
    with get_session() as session:
        total = session.query(SyncLog).count()
        recent = session.query(SyncLog).order_by(
            SyncLog.start_time.desc()
        ).limit(limit).all()

        items = [
            SyncStatusItem.model_validate(log) for log in recent
        ]

        return SyncStatusResponse(
            recent_syncs=items,
            total_syncs=total,
        )


@app.get("/stats", response_model=DashboardStats, tags=["统计"])
async def get_stats():
    """获取数据统计"""
    with get_session() as session:
        return DashboardStats(
            total_stocks=session.query(StockInfo).count(),
            cn_a_stocks=session.query(StockInfo).filter(StockInfo.market == "CN_A").count(),
            hk_stocks=session.query(StockInfo).filter(StockInfo.market == "HK").count(),
            financial_indicators=session.query(FinancialIndicator).count(),
            income_statements=session.query(IncomeStatement).count(),
            balance_sheets=session.query(BalanceSheet).count(),
            cash_flow_statements=session.query(CashFlowStatement).count(),
            index_constituents=session.query(IndexConstituent).count(),
        )


@app.get("/stocks", response_model=List[StockInfoOut], tags=["股票信息"])
async def list_stocks(
    market: Optional[str] = Query(None, description="市场：CN_A/HK"),
    exchange: Optional[str] = Query(None, description="交易所：shanghai/shenzhen/hk"),
    industry: Optional[str] = Query(None, description="行业"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """查询股票列表"""
    with get_session() as session:
        query = session.query(StockInfo)
        
        if market:
            query = query.filter(StockInfo.market == market)
        if exchange:
            query = query.filter(StockInfo.exchange == exchange)
        if industry:
            query = query.filter(StockInfo.industry == industry)
        
        stocks = query.limit(limit).offset(offset).all()
        return [StockInfoOut.model_validate(s) for s in stocks]


@app.get("/stocks/{stock_code}", response_model=StockInfoOut, tags=["股票信息"])
async def get_stock(stock_code: str):
    """查询单只股票信息"""
    with get_session() as session:
        stock = session.query(StockInfo).filter(StockInfo.stock_code == stock_code).first()
        if not stock:
            raise HTTPException(status_code=404, detail=f"Stock {stock_code} not found")
        return StockInfoOut.model_validate(stock)


@app.get("/stocks/{stock_code}/indicators", response_model=List[FinancialIndicatorOut], tags=["财务指标"])
async def get_indicators(
    stock_code: str,
    limit: int = Query(52, ge=1, le=200),  # 默认最近52周
):
    """查询股票财务指标"""
    with get_session() as session:
        indicators = session.query(FinancialIndicator).filter(
            FinancialIndicator.stock_code == stock_code
        ).order_by(
            desc(FinancialIndicator.indicator_date)
        ).limit(limit).all()
        
        return [FinancialIndicatorOut.model_validate(i) for i in indicators]


@app.get("/stocks/{stock_code}/income", response_model=List[IncomeStatementOut], tags=["财务报表"])
async def get_income_statement(
    stock_code: str,
    limit: int = Query(8, ge=1, le=20),  # 默认最近8个季度
):
    """查询利润表"""
    with get_session() as session:
        statements = session.query(IncomeStatement).filter(
            IncomeStatement.stock_code == stock_code
        ).order_by(
            desc(IncomeStatement.report_date)
        ).limit(limit).all()
        
        return [IncomeStatementOut.model_validate(s) for s in statements]


@app.get("/indices/{index_code}/constituents", response_model=List[IndexConstituentOut], tags=["指数成分"])
async def get_index_constituents(
    index_code: str,
    active_only: bool = Query(True, description="仅显示当前在位的成分股"),
):
    """查询指数成分股"""
    with get_session() as session:
        query = session.query(IndexConstituent).filter(
            IndexConstituent.index_code == index_code
        )
        
        if active_only:
            query = query.filter(IndexConstituent.is_active == 1)
        
        constituents = query.all()
        return [IndexConstituentOut.model_validate(c) for c in constituents]


@app.get("/search", response_model=List[StockInfoOut], tags=["搜索"])
async def search_stocks(
    keyword: str = Query(..., min_length=1, description="搜索关键词（代码或名称）"),
    limit: int = Query(20, ge=1, le=100),
):
    """搜索股票（按代码或名称）"""
    with get_session() as session:
        stocks = session.query(StockInfo).filter(
            (StockInfo.stock_code.like(f"%{keyword}%")) |
            (StockInfo.stock_name.like(f"%{keyword}%"))
        ).limit(limit).all()
        
        return [StockInfoOut.model_validate(s) for s in stocks]


# ============================================================
# 主入口
# ============================================================

def run_server():
    """运行API服务"""
    import uvicorn
    uvicorn.run(
        "api:app",
        host=config.API_CONFIG["host"],
        port=config.API_CONFIG["port"],
        reload=config.API_CONFIG["reload"],
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_server()
