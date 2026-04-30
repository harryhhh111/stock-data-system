"""FastAPI 应用工厂"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from web.routes import health, dashboard, sync, quality, screener, analyzer


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
        ],
        allow_origin_regex=r"https://.*\.pages\.dev",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册路由
    app.include_router(health.router, prefix="/api/v1", tags=["health"])
    app.include_router(dashboard.router, prefix="/api/v1", tags=["dashboard"])
    app.include_router(sync.router, prefix="/api/v1", tags=["sync"])
    app.include_router(quality.router, prefix="/api/v1", tags=["quality"])
    app.include_router(screener.router, prefix="/api/v1", tags=["screener"])
    app.include_router(analyzer.router, prefix="/api/v1", tags=["analyzer"])

    return app


app = create_app()