"""FastAPI 应用工厂"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

import config
from web.routes import health, dashboard, sync, quality, screener, analyzer, backtest, paper, strategy, storyline
from web.routes import watchlist, hyperliquid


def create_app() -> FastAPI:
    app = FastAPI(
        title="Stock Data API",
        version="1.0.0",
        description="JSON API for stock data dashboard",
    )

    # CORS：允许 Cloudflare Pages + localhost dev
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://localhost:5174",
        ],
        allow_origin_regex=r"https://.*\.pages\.dev",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 响应压缩（§10 合同：级别 5、最小 1024B、框架标准中间件）。
    # 进程级开关，默认关闭；只应在美股后端进程启用 STOCK_HTTP_GZIP_ENABLED=true。
    # 开启后影响本进程内所有符合条件的 HTTP 响应（不止 news 两个接口）。
    if config.http.gzip_enabled:
        app.add_middleware(GZipMiddleware, compresslevel=5, minimum_size=1024)

    # 注册路由
    app.include_router(health.router, prefix="/api/v1", tags=["health"])
    app.include_router(dashboard.router, prefix="/api/v1", tags=["dashboard"])
    app.include_router(sync.router, prefix="/api/v1", tags=["sync"])
    app.include_router(quality.router, prefix="/api/v1", tags=["quality"])
    app.include_router(screener.router, prefix="/api/v1", tags=["screener"])
    app.include_router(analyzer.router, prefix="/api/v1", tags=["analyzer"])
    app.include_router(backtest.router, prefix="/api/v1", tags=["backtest"])
    app.include_router(paper.router, prefix="/api/v1", tags=["paper"])
    app.include_router(strategy.router, prefix="/api/v1", tags=["strategy"])
    app.include_router(storyline.router, prefix="/api/v1", tags=["storyline"])
    app.include_router(watchlist.router, prefix="/api/v1", tags=["watchlist"])
    app.include_router(hyperliquid.router, prefix="/api/v1", tags=["hyperliquid"])

    return app


app = create_app()
