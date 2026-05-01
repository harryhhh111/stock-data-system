# Phase 4 前端部署进度

> 开始时间：2026-05-01
> 状态：✅ 就绪（Pre-checks 通过）

---

## 已完成 ✅

### Phase 1: API 骨架 + Dashboard
- [x] requirements.txt 添加 fastapi, uvicorn[standard]
- [x] 虚拟环境安装所有依赖
- [x] FastAPI 应用工厂（web/app.py）
- [x] Health check 端点
- [x] Dashboard API 端点
- [x] Dashboard service（数据库聚合查询）
- [x] uvicorn 启动入口（web/__main__.py）
- [x] API 服务器测试通过

### Phase 2: 前端骨架 + Dashboard
- [x] React + Vite + TypeScript 框架搭建
- [x] shadcn/ui 组件库初始化
- [x] API 客户端（lib/api/client.ts）- 双服务器路由
- [x] TypeScript 类型定义（lib/types/）
- [x] TanStack Query hooks（lib/hooks/）
- [x] Dashboard 页面实现
- [x] Dashboard 组件（StatCard, SyncPieChart, SyncTrendChart 等）
- [x] 前端构建成功
- [x] 前端开发服务器测试通过

### Phase 3: 其余页面
- [x] Sync API + 页面
- [x] Quality API + 页面
- [x] Screener API + 页面
- [x] Analyzer API + 页面
- [x] 所有页面路由配置

### Phase 4: 部署准备
- [x] Nginx 配置文件（scripts/nginx-stock-api.conf）
- [x] systemd 服务文件（scripts/stock-web.service）
- [x] 完整部署文档（docs/PHASE4_DEPLOYMENT.md）
- [x] 部署前检查脚本（scripts/check_deployment.sh）
- [x] 所有预检查通过

---

## 待完成 📋

### Part 1: API 服务器部署
- [ ] 安装 Nginx（`sudo apt install nginx certbot python3-certbot-nginx`）
- [ ] 配置 Nginx 反向代理
- [ ] 配置 HTTPS（Let's Encrypt，可选但推荐）
- [ ] 安装 systemd 服务
- [ ] 启动服务并验证

### Part 2: 前端部署
- [ ] 准备 Git 仓库（确保 frontend/ 已提交）
- [ ] 连接 Cloudflare Pages
- [ ] 配置构建设置
- [ ] 设置环境变量（VITE_CN_API_URL, VITE_US_API_URL）
- [ ] 触发首次部署

### Part 3: 验证与监控
- [ ] 端到端测试
- [ ] 配置日志监控
- [ ] 设置告警（可选）

---

## 文件清单

### 后端 API
- `web/app.py` - FastAPI 应用工厂
- `web/__main__.py` - uvicorn 启动入口
- `web/routes/` - API 路由
- `web/services/` - 业务逻辑
- `web/wrappers/` - CLI 包装器

### 前端
- `frontend/` - React SPA
  - `src/App.tsx` - 路由配置
  - `src/pages/` - 页面组件
  - `src/components/` - UI 组件
  - `src/lib/api/` - API 客户端
  - `src/lib/types/` - TypeScript 类型
  - `src/lib/hooks/` - React hooks

### 部署配置
- `scripts/nginx-stock-api.conf` - Nginx 配置
- `scripts/stock-web.service` - systemd 服务
- `scripts/check_deployment.sh` - 部署前检查
- `docs/PHASE4_DEPLOYMENT.md` - 部署指南

---

## 快速部署命令

```bash
# 1. 运行预检查
./scripts/check_deployment.sh

# 2. 按照 docs/PHASE4_DEPLOYMENT.md 完成部署

# 3. 验证部署
curl https://api.us.stock.example.com/api/v1/health
curl https://your-project.pages.dev
```

---

## 架构图

```
用户浏览器
    │
    └── https://dashboard.stock.example.com (Cloudflare Pages)
        │
        │  VITE_CN_API_URL = https://api.cn.stock.example.com
        │  VITE_US_API_URL = https://api.us.stock.example.com
        │
        ├── CN_A / CN_HK 请求 → 国内服务器
        │   └── Nginx → 127.0.0.1:8000 (uvicorn) → PostgreSQL
        │
        └── US 请求 → 海外服务器
            └── Nginx → 127.0.0.1:8000 (uvicorn) → PostgreSQL
```

---

## 注意事项

1. **域名配置**：确保 DNS 已指向正确的服务器 IP
2. **SSL 证书**：强烈建议配置 HTTPS，特别是生产环境
3. **环境变量**：Cloudflare Pages 需要配置正确的 API URL
4. **CORS 配置**：已在 `web/app.py` 中配置，无需额外设置
5. **防火墙**：确保端口 80、443 已开放

---

## 相关文档

- [WEB_FRONTEND_PLAN.md](../docs/quant/WEB_FRONTEND_PLAN.md) - 前端设计文档
- [PHASE4_DEPLOYMENT.md](../docs/PHASE4_DEPLOYMENT.md) - 详细部署指南
- [QUANT_SYSTEM_PLAN.md](../docs/quant/QUANT_SYSTEM_PLAN.md) - 量化系统规划

---

## 支持与反馈

如遇问题，请查看：
1. 日志：`sudo journalctl -u stock-web -f`
2. Nginx 日志：`sudo tail -f /var/log/nginx/error.log`
3. 浏览器控制台（前端）
4. 部署文档中的故障排查部分
