"""响应压缩合同验证（docs/WATCHLIST_DAILY_NEWS_MVP_PLAN.md §10.3 验证表）。

走 web.app.create_app 实际注册路径；探针路由注入创建的 app 实例，
不依赖数据库。固定数据样本下对比关闭/开启两种模式的响应。
"""
import subprocess
import sys

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import config
import web.app

_PROBE_BODY = {"payload": "x" * 2000}


def _probe_app():
    """真实应用工厂 + 免 DB 探针路由（大响应 / 小响应 / 错误响应）。"""
    app = web.app.create_app()

    @app.get("/__gzip_probe")
    def probe():
        return _PROBE_BODY

    @app.get("/__gzip_probe_small")
    def probe_small():
        return {"ok": True}

    @app.get("/__gzip_probe_error")
    def probe_error():
        raise HTTPException(status_code=418, detail="teapot")

    return app


@pytest.fixture
def gzip_enabled(monkeypatch):
    monkeypatch.setattr(config.http, "gzip_enabled", True)


def _middleware_names(app) -> list[str]:
    return [m.cls.__name__ for m in app.user_middleware]


class TestMiddlewareRegistration:
    def test_default_off_no_gzip_middleware(self):
        app = web.app.create_app()
        assert "GZipMiddleware" not in _middleware_names(app)

    def test_enabled_registers_gzip_middleware(self, gzip_enabled):
        app = web.app.create_app()
        assert "GZipMiddleware" in _middleware_names(app)

    def test_env_var_parsing_default_off(self):
        out = subprocess.run(
            [sys.executable, "-c", "import config; print(config.http.gzip_enabled)"],
            capture_output=True, text=True, check=True, cwd=config.PROJECT_ROOT)
        assert out.stdout.strip() == "False"

    def test_env_var_parsing_enabled(self):
        import os
        env = {**os.environ, "STOCK_HTTP_GZIP_ENABLED": "true"}
        out = subprocess.run(
            [sys.executable, "-c", "import config; print(config.http.gzip_enabled)"],
            capture_output=True, text=True, check=True, cwd=config.PROJECT_ROOT,
            env=env)
        assert out.stdout.strip() == "True"


class TestDisabledModeUnchanged:
    def test_gzip_accepting_client_gets_identity(self):
        client = TestClient(_probe_app())
        r = client.get("/__gzip_probe", headers={"Accept-Encoding": "gzip"})
        assert r.status_code == 200
        assert "content-encoding" not in r.headers
        assert r.json() == _PROBE_BODY


class TestEnabledMode:
    def test_large_json_compressed_and_identical(self, gzip_enabled):
        client = TestClient(_probe_app())
        r = client.get("/__gzip_probe", headers={"Accept-Encoding": "gzip"})
        assert r.status_code == 200
        assert r.headers["content-encoding"] == "gzip"
        assert "accept-encoding" in r.headers["vary"].lower()
        assert r.headers["content-type"].startswith("application/json")
        # 解压后 JSON 与关闭压缩时逐字节一致（httpx 已自动解压）
        assert r.json() == _PROBE_BODY
        assert r.content == (b'{"payload":"' + b"x" * 2000 + b'"}')

    def test_identity_client_still_readable(self, gzip_enabled):
        client = TestClient(_probe_app())
        r = client.get("/__gzip_probe", headers={"Accept-Encoding": "identity"})
        assert r.status_code == 200
        assert "content-encoding" not in r.headers
        assert r.json() == _PROBE_BODY

    def test_small_response_below_threshold(self, gzip_enabled):
        client = TestClient(_probe_app())
        r = client.get("/__gzip_probe_small", headers={"Accept-Encoding": "gzip"})
        assert r.status_code == 200
        assert "content-encoding" not in r.headers
        assert r.json() == {"ok": True}

    def test_error_response_unaffected(self, gzip_enabled):
        client = TestClient(_probe_app())
        r = client.get("/__gzip_probe_error", headers={"Accept-Encoding": "gzip"})
        assert r.status_code == 418
        assert r.json() == {"detail": "teapot"}

    def test_cors_behavior_preserved(self, gzip_enabled):
        client = TestClient(_probe_app())
        r = client.options("/__gzip_probe", headers={
            "Origin": "https://foo.pages.dev",
            "Access-Control-Request-Method": "GET",
        })
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "https://foo.pages.dev"

    def test_no_double_compress(self, gzip_enabled):
        """既有 Content-Encoding 的响应不得二次压缩。

        上游返回合法 gzip 体：若被二次压缩，客户端解压一层后得到的是
        内层 gzip 字节而非原 JSON；断言 r.json() 即可区分。
        """
        import gzip as gzip_mod

        app = web.app.create_app()
        inner = gzip_mod.compress(b'{"pre":true}')

        @app.get("/__gzip_pre_encoded")
        def pre_encoded():
            from fastapi import Response
            return Response(content=inner,
                            media_type="application/json",
                            headers={"Content-Encoding": "gzip"})

        client = TestClient(app)
        r = client.get("/__gzip_pre_encoded", headers={"Accept-Encoding": "gzip"})
        assert r.headers["content-encoding"] == "gzip"
        assert r.json() == {"pre": True}
        assert len(r.content) == len(b'{"pre":true}')
