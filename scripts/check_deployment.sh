#!/bin/bash
# 部署前验证脚本
# 检查所有必需的组件是否已就绪

set -e

# 进程清理：脚本退出时自动 kill 所有后台进程
cleanup() {
    if [ -n "$WEB_PID" ] && kill -0 $WEB_PID 2>/dev/null; then
        kill $WEB_PID 2>/dev/null
        wait $WEB_PID 2>/dev/null
    fi
}
trap cleanup EXIT INT TERM

echo "=== Phase 4 部署前检查 ==="
echo ""

# 1. 检查 Python 依赖
echo "1. 检查 Python 依赖..."
if venv/bin/python -c "import fastapi, uvicorn" 2>/dev/null; then
    echo "✅ FastAPI 和 Uvicorn 已安装"
else
    echo "❌ FastAPI 或 Uvicorn 未安装"
    exit 1
fi

# 2. 检查数据库连接
echo "2. 检查数据库连接..."
if venv/bin/python -c "import db; db.get_connection()" 2>/dev/null; then
    echo "✅ 数据库连接正常"
else
    echo "❌ 数据库连接失败"
    exit 1
fi

# 3. 检查 API 服务器
echo "3. 测试 API 服务器..."
venv/bin/python -m web --host 0.0.0.0 --port 8000 > /tmp/web_test.log 2>&1 &
WEB_PID=$!
sleep 3

if curl -s http://localhost:8000/api/v1/health | grep -q '"ok":true'; then
    echo "✅ API 服务器运行正常"
else
    echo "❌ API 服务器启动失败"
    cat /tmp/web_test.log
    exit 1
fi
kill $WEB_PID 2>/dev/null; wait $WEB_PID 2>/dev/null

# 4. 检查前端构建
echo "4. 测试前端构建..."
cd frontend
if npm run build > /tmp/frontend_build.log 2>&1; then
    echo "✅ 前端构建成功"
else
    echo "❌ 前端构建失败"
    cat /tmp/frontend_build.log
    exit 1
fi
cd ..

# 5. 检查 API 端点
echo "5. 测试 API 端点..."
venv/bin/python -m web --host 0.0.0.0 --port 8000 > /tmp/web_test.log 2>&1 &
WEB_PID=$!
sleep 3

# 测试 dashboard
if curl -s http://localhost:8000/api/v1/dashboard/stats | grep -q '"ok":true'; then
    echo "✅ /api/v1/dashboard/stats 正常"
else
    echo "❌ /api/v1/dashboard/stats 失败"
    exit 1
fi

# 6. 检查配置文件
echo "6. 检查部署配置文件..."
if [ -f "scripts/nginx-stock-api.conf" ]; then
    echo "✅ Nginx 配置文件存在"
else
    echo "❌ Nginx 配置文件缺失"
    exit 1
fi

if [ -f "scripts/stock-web.service" ]; then
    echo "✅ systemd 服务文件存在"
else
    echo "❌ systemd 服务文件缺失"
    exit 1
fi

 if [ -f "docs/deployment/PHASE4_DEPLOYMENT.md" ]; then
    echo "✅ 部署文档存在"
else
    echo "❌ 部署文档缺失"
    exit 1
fi

 echo ""
 echo "=== 所有检查通过！可以开始部署 ==="
 echo ""
 echo "下一步："
 echo "1. 查看 docs/deployment/PHASE4_DEPLOYMENT.md 获取详细部署指南"
 echo "2. 按照指南完成 Nginx + systemd 配置"
 echo "3. 连接 Cloudflare Pages 部署前端"
