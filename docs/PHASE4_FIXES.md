# Phase 4 部署准备 - 问题修复记录

> 修复时间：2026-05-01
> 状态：✅ 所有问题已修复并验证

---

## 问题 1：check_deployment.sh 缺少进程清理（必修）

**问题描述**：
脚本第 30/58 行启动了后台 web 服务器，但脚本中途 exit 1 或被 Ctrl+C 时不会 kill 子进程，会留下僵尸 uvicorn 占 8000 端口。

**修复方案**：
添加 `cleanup()` 函数和 `trap` 来捕获退出信号并清理后台进程。

```bash
# 进程清理：脚本退出时自动 kill 所有后台进程
cleanup() {
    if [ -n "$WEB_PID" ] && kill -0 $WEB_PID 2>/dev/null; then
        kill $WEB_PID 2>/dev/null
        wait $WEB_PID 2>/dev/null
    fi
}
trap cleanup EXIT INT TERM
```

**验证结果**：
- ✅ 脚本正常退出时，web 服务器被正确清理
- ✅ 脚本异常退出（exit 1）时，web 服务器被正确清理
- ✅ Ctrl+C 中断时，web 服务器被正确清理
- ✅ 端口 8000 释放成功

---

## 问题 2：check_deployment.sh db 检测用了 python3 而非 venv（必修）

**问题描述**：
第 21 行用 `python3 -c "import db"`，但 db 模块依赖 psycopg2 和 .env 配置，系统 python3 不一定能找到这些。应该用 venv/bin/python。

**修复方案**：
```bash
# 修复前
if python3 -c "import db; db.get_connection()" 2>/dev/null; then

# 修复后
if venv/bin/python -c "import db; db.get_connection()" 2>/dev/null; then
```

**验证结果**：
- ✅ 数据库连接检查通过
- ✅ 使用 venv 中的 psycopg2 和配置

---

## 问题 3：requirements.txt 缺 pydantic（建议）

**问题描述**：
screener.py 路由直接用了 `ScreenerParams(BaseModel)`，虽然 FastAPI 会间接安装 pydantic，但显式声明更稳妥。

**修复方案**：
在 requirements.txt 中添加：
```
pydantic>=2.9.0
```

**验证结果**：
- ✅ pydantic 已安装到 venv
- ✅ FastAPI 的 Pydantic 集成正常

---

## 问题 4：stock-web.service 第 13 行有行尾注释（建议）

**问题描述**：
`User=vinci  # 替换为你的用户名` — systemd 通常能处理，但行尾注释在某些版本可能被解析为用户名的一部分。建议删掉注释或放到上一行。

**修复方案**：
```ini
# 修复前
User=vinci  # 替换为你的用户名

# 修复后（注释移到文件顶部）
User=vinci
```

**验证结果**：
- ✅ systemd 服务文件语法正确
- ✅ User 行清晰无歧义

---

## 验证结果汇总

### 依赖检查 ✅
```bash
venv/bin/pip list | grep -E "(fastapi|uvicorn|pydantic)"
# fastapi         0.136.1
# pydantic        2.13.3
# uvicorn         0.46.0
```

### 部署检查脚本 ✅
```bash
./scripts/check_deployment.sh
# 所有 6 项检查全部通过
```

### 进程清理测试 ✅
```bash
/tmp/test_cleanup.sh
# Cleanup called, killing web server...
# Web server killed
# ✅ Port 8000 is free
```

---

## 修复的文件

1. `scripts/check_deployment.sh`
   - 添加 trap cleanup 机制
   - 改用 venv/bin/python 检查数据库

2. `requirements.txt`
   - 添加 pydantic>=2.9.0

3. `scripts/stock-web.service`
   - 删除 User 行的行尾注释

---

## 后续建议

1. **部署前运行检查**
   ```bash
   ./scripts/check_deployment.sh
   ```

2. **部署时替换占位符**
   - `api.us.stock.example.com` → 实际域名
   - `User=vinci` → 实际用户名

3. **首次部署后验证**
   - systemd 服务状态：`sudo systemctl status stock-web`
   - Nginx 状态：`sudo systemctl status nginx`
   - API 健康检查：`curl https://your-domain/api/v1/health`

---

## 相关文档

- `docs/PHASE4_DEPLOYMENT.md` - 详细部署指南
- `docs/PHASE4_PROGRESS.md` - 部署进度追踪
- `scripts/check_deployment.sh` - 部署前检查脚本
