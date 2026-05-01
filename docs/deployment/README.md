# 部署文档

> 部署相关文档的导航入口

## 快速开始

- **首次部署** → [PHASE4_DEPLOYMENT.md](PHASE4_DEPLOYMENT.md) - 前端部署完整指南

## 文档列表

| 文档 | 内容 | 状态 |
|------|------|------|
| [PHASE4_DEPLOYMENT.md](PHASE4_DEPLOYMENT.md) | Nginx + systemd + Cloudflare Pages 部署指南 | ✅ 就绪 |
| [PHASE4_FIXES.md](PHASE4_FIXES.md) | Phase 4 部署问题修复记录 | ✅ 已完成 |
| [PHASE4_PROGRESS.md](PHASE4_PROGRESS.md) | Phase 4 部署进度追踪 | ✅ 已完成 |

## 部署架构

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

## 部署检查清单

- [ ] 部署前运行检查脚本：`./scripts/check_deployment.sh`
- [ ] 按照 PHASE4_DEPLOYMENT.md 完成 Nginx 配置
- [ ] 配置 systemd 服务并启动
- [ ] 连接 Cloudflare Pages 并配置环境变量
- [ ] 端到端测试通过

## 相关文档

- [../README.md](../README.md) - 总文档导航
- [../quant/WEB_FRONTEND_PLAN.md](../quant/WEB_FRONTEND_PLAN.md) - 前端设计文档
- [../core/ARCHITECTURE.md](../core/ARCHITECTURE.md) - 系统架构
