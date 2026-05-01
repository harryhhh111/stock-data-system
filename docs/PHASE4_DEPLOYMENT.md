# Phase 4: 前端部署指南

## 概述

本指南涵盖 Web 前端的完整部署流程：
- API 服务器：Nginx 反代 + HTTPS + systemd 管理
- 前端：Cloudflare Pages（一份构建产物访问两个 API 服务器）

## 前置条件

- 服务器：Ubuntu 20.04+ / Debian 11+
- 域名：已配置 DNS 指向服务器 IP
- PostgreSQL：已安装并运行
- Python 虚拟环境：已配置并安装依赖
- Cloudflare 账号（用于前端托管）

---

## Part 1: API 服务器部署

### 1.1 安装 Nginx

```bash
sudo apt update
sudo apt install nginx certbot python3-certbot-nginx -y
```

### 1.2 配置 Nginx 反向代理

```bash
# 复制配置文件
sudo cp scripts/nginx-stock-api.conf /etc/nginx/sites-available/stock-api

# 修改域名（替换 api.us.stock.example.com 为你的实际域名）
sudo nano /etc/nginx/sites-available/stock-api

# 启用站点
sudo ln -s /etc/nginx/sites-available/stock-api /etc/nginx/sites-enabled/

# 测试配置
sudo nginx -t
```

### 1.3 配置 HTTPS（可选但推荐）

```bash
# 自动获取并配置 SSL 证书
sudo certbot --nginx -d api.us.stock.example.com

# Certbot 会自动修改 Nginx 配置，启用 HTTPS
```

### 1.4 配置 systemd 服务

```bash
# 复制服务文件
sudo cp scripts/stock-web.service /etc/systemd/system/

# 修改用户名（替换 vinci 为你的实际用户名）
sudo nano /etc/systemd/system/stock-web.service

# 重载 systemd 配置
sudo systemctl daemon-reload

# 启动服务
sudo systemctl start stock-web

# 设置开机自启
sudo systemctl enable stock-web

# 查看服务状态
sudo systemctl status stock-web

# 查看日志
sudo journalctl -u stock-web -f
```

### 1.5 测试 API

```bash
# 本地测试
curl http://localhost:8000/api/v1/health

# 通过域名测试（如果已配置 DNS）
curl http://api.us.stock.example.com/api/v1/health

# 测试 dashboard 端点
curl http://api.us.stock.example.com/api/v1/dashboard/stats
```

---

## Part 2: 前端部署（Cloudflare Pages）

### 2.1 准备 Git 仓库

```bash
# 确保项目在 Git 仓库中
cd /home/vinci/projects/stock_data
git add frontend/
git commit -m "Add frontend build"
git push
```

### 2.2 连接 Cloudflare Pages

1. 登录 [Cloudflare Dashboard](https://dash.cloudflare.com/)
2. 进入 **Workers & Pages** → **Create application** → **Pages**
3. 选择 **Connect to Git**
4. 授权访问你的 GitHub 仓库
5. 选择 `stock_data` 仓库
6. 配置构建设置：
   - **Framework preset**: `Vite`
   - **Build command**: `npm run build`
   - **Build output directory**: `frontend/dist`
   - **Root directory**: `/`（或 `frontend/` 如果 frontend 是子目录）

### 2.3 配置环境变量

在 Cloudflare Pages 项目设置中，添加以下环境变量：

| 变量名 | 值 | 说明 |
|--------|-----|------|
| `VITE_CN_API_URL` | `https://api.cn.stock.example.com` | 国内 API 服务器（国内服务器部署） |
| `VITE_US_API_URL` | `https://api.us.stock.example.com` | 海外 API 服务器（海外服务器部署） |

**注意**：
- 如果只有一台服务器，两个变量可以设置为相同的值
- 必须是 HTTPS 地址（Cloudflare Pages 强制 HTTPS）
- 不要在 URL 后面加 `/`

### 2.4 部署

1. 保存设置后，Cloudflare 会自动触发首次部署
2. 等待构建完成（约 1-2 分钟）
3. 获得部署域名：`https://your-project.pages.dev`
4. （可选）配置自定义域名

### 2.5 配置自定义域名

1. 在 Cloudflare Pages 项目设置中，点击 **Custom domains**
2. 添加你的域名（如 `dashboard.stock.example.com`）
3. Cloudflare 会自动配置 DNS 和 SSL

---

## Part 3: 验证部署

### 3.1 验证 API 服务器

```bash
# 检查 systemd 服务状态
sudo systemctl status stock-web

# 检查 Nginx 状态
sudo systemctl status nginx

# 测试 API 端点
curl -I https://api.us.stock.example.com/api/v1/health
```

### 3.2 验证前端

1. 打开浏览器访问 `https://your-project.pages.dev`
2. 检查浏览器控制台是否有错误
3. 验证仪表板数据是否正常显示
4. 测试各页面导航

### 3.3 端到端测试

```bash
# 测试健康检查
curl https://your-project.pages.dev/api/v1/health

# 测试 dashboard 数据
curl https://your-project.pages.dev/api/v1/dashboard/stats | jq

# 测试筛选器预设
curl -X POST https://your-project.pages.dev/api/v1/screener/run \
  -H "Content-Type: application/json" \
  -d '{"market":"US","preset":"classic_value","top_n":5}' | jq
```

---

## Part 4: 监控与维护

### 4.1 查看日志

```bash
# API 服务日志
sudo journalctl -u stock-web -f

# Nginx 访问日志
sudo tail -f /var/log/nginx/access.log

# Nginx 错误日志
sudo tail -f /var/log/nginx/error.log
```

### 4.2 重启服务

```bash
# 重启 API 服务
sudo systemctl restart stock-web

# 重启 Nginx
sudo systemctl restart nginx

# 重新加载 Nginx 配置（不中断服务）
sudo nginx -s reload
```

### 4.3 SSL 证书续期

```bash
# Certbot 会自动续期，但可以手动测试
sudo certbot renew --dry-run

# 查看续期日志
sudo cat /var/log/letsencrypt/letsencrypt.log
```

### 4.4 更新前端

```bash
# 推送新代码到 GitHub
git add .
git commit -m "Update frontend"
git push

# Cloudflare Pages 会自动触发重新部署
```

---

## 故障排查

### API 服务无法启动

```bash
# 查看详细错误信息
sudo journalctl -u stock-web -n 50

# 常见问题：
# 1. 端口 8000 被占用：sudo lsof -i :8000
# 2. 权限问题：检查 systemd 文件中的 User/Group
# 3. 虚拟环境路径错误：检查 ExecStart 中的路径
```

### Nginx 502 Bad Gateway

```bash
# 检查 uvicorn 是否运行
sudo systemctl status stock-web

# 检查防火墙
sudo ufw status
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
```

### 前端无法连接 API

1. 检查浏览器控制台的网络请求
2. 验证 CORS 配置（在 `web/app.py` 中）
3. 检查环境变量是否正确设置
4. 确认 API 服务器的 HTTPS 证书有效

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

## 部署检查清单

- [ ] Nginx 已安装并运行
- [ ] SSL 证书已配置（可选但推荐）
- [ ] systemd 服务已创建并启动
- [ ] API 端点测试通过
- [ ] GitHub 仓库已推送
- [ ] Cloudflare Pages 已连接
- [ ] 环境变量已配置
- [ ] 前端构建成功
- [ ] 自定义域名已配置（可选）
- [ ] 端到端测试通过
- [ ] 监控日志已配置

---

## 参考文档

- [Nginx 官方文档](https://nginx.org/en/docs/)
- [systemd 官方文档](https://www.freedesktop.org/software/systemd/man/systemd.service.html)
- [Cloudflare Pages 文档](https://developers.cloudflare.com/pages/)
- [Certbot 文档](https://certbot.eff.org/docs/)
