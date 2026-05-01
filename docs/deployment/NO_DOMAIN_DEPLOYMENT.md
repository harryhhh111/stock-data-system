# 无域名部署方案

> 使用 Cloudflare Tunnel 免费暴露 API，无需购买域名

## 概述

如果你的服务器没有购买域名，可以使用 Cloudflare Tunnel 免费暴露 API 到公网，并获得 HTTPS 访问。

**方案对比**：
- ✅ **Cloudflare Tunnel**：完全免费，自动 HTTPS，稳定
- ⚠️ **ngrok**：免费但有流量限制，域名不稳定
- ❌ **仅 HTTP**：无法与 Cloudflare Pages（HTTPS）通信

---

## 方案 1：Cloudflare Tunnel（推荐）

### 1.1 安装 cloudflared

```bash
# 下载最新版本（Linux x86_64）
wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64

# 安装
sudo mv cloudflared-linux-amd64 /usr/local/bin/cloudflared
sudo chmod +x /usr/local/bin/cloudflared

# 验证安装
cloudflared --version
```

### 1.2 登录并创建 Tunnel

```bash
# 登录（会打开浏览器授权）
cloudflared tunnel login

# 创建 tunnel（随意起名）
cloudflared tunnel create stock-api

# 记住输出的 Tunnel ID，如：abc12345-def6-7890-ghij-klmnopqrst
```

### 1.3 配置反向代理

```bash
# 创建配置目录
mkdir -p ~/.cloudflared

# 创建配置文件
cat > ~/.cloudflared/config.yml << 'EOF'
tunnel: abc12345-def6-7890-ghij-klmnopqrst  # 替换为你的 Tunnel ID

ingress:
  - service: http://localhost:8000
  - service: http_status:404
EOF
```

### 1.4 启动 Tunnel

```bash
# 测试启动（前台运行）
cloudflared tunnel --config ~/.cloudflared/config.yml run stock-api

# 记下输出的临时域名，如：https://abc12345.trycloudflare.com
```

### 1.5 作为 systemd 服务运行

```bash
# 创建 systemd 服务文件
sudo tee /etc/systemd/system/cloudflared-stock-api.service > /dev/null << 'EOF'
[Unit]
Description=Cloudflare Tunnel for Stock API
After=network.target

[Service]
Type=simple
User=vinci
ExecStart=/usr/local/bin/cloudflared tunnel --config /home/vinci/.cloudflared/config.yml run stock-api
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 启动服务
sudo systemctl daemon-reload
sudo systemctl start cloudflared-stock-api
sudo systemctl enable cloudflared-stock-api

# 查看状态
sudo systemctl status cloudflared-stock-api

# 查看日志
sudo journalctl -u cloudflared-stock-api -f
```

### 1.6 测试访问

```bash
# 测试临时域名（HTTPS）
curl https://abc12345.trycloudflare.com/api/v1/health

# 预期输出：
# {"ok":true,"data":{"db":true}}
```

### 1.7 更新 Cloudflare Pages 环境变量

在 Cloudflare Pages 项目设置中：

| 变量名 | 值 |
|--------|-----|
| `VITE_US_API_URL` | `https://abc12345.trycloudflare.com` |

---

## 方案 2：ngrok（临时测试）

### 2.1 安装

```bash
# 下载 ngrok（Linux x86_64）
wget https://bin.equinox.io/c/4VmDzA7iaHb/ngrok-stable-linux-amd64.zip
unzip ngrok-stable-linux-amd64.zip
sudo mv ngrok /usr/local/bin/ngrok
```

### 2.2 启动

```bash
# 将 8000 端口暴露到公网
ngrok http 8000

# 会输出：
# Forwarding  https://abc12345.ngrok.io -> http://localhost:8000
```

### 2.3 更新 Cloudflare Pages 环境变量

| 变量名 | 值 |
|--------|-----|
| `VITE_US_API_URL` | `https://abc12345.ngrok.io` |

**缺点**：
- ❌ 免费版有流量限制（1GB/月）
- ❌ 域名每次启动都会变化
- ❌ 不适合长期使用

---

## 域名方案对比

| 特性 | 购买域名 | Cloudflare Tunnel | ngrok |
|------|---------|-----------------|-------|
| **成本** | ~$10-50/年 | 免费 | 免费（有限制）|
| **HTTPS** | ✅ | ✅ | ✅ |
| **稳定性** | ✅ | ✅ | ⚠️（域名会变）|
| **自定义域名** | ✅ | ✅（可选）| ❌ |
| **公网 IP** | 需要 | 不需要 | 不需要 |
| **适用场景** | 生产环境 | 开发/测试 | 临时测试 |

---

## 部署检查清单（无域名版本）

- [ ] cloudflared 已安装
- [ ] Tunnel 已创建并记录 ID
- [ ] 配置文件已创建（`~/.cloudflared/config.yml`）
- [ ] systemd 服务已创建并启动
- [ ] 临时域名测试通过
- [ ] uvicorn 服务运行中（`systemctl status stock-web`）
- [ ] Cloudflare Pages 环境变量已更新
- [ ] 端到端测试通过

---

## 从 Tunnel 迁移到域名

如果你后续购买了域名，可以迁移：

### 步骤 1：为 Tunnel 配置自定义域名

```bash
# 在 Cloudflare Dashboard 中：
# 1. 进入 Workers & Pages → Zero Trust → Networks → Tunnels
# 2. 找到你的 tunnel，点击 Configure
# 3. 在 Public Hostname 中添加：
#    Subdomain: api
#    Domain: your-domain.com
#    Service: http://localhost:8000
```

### 步骤 2：更新 Cloudflare Pages 环境变量

| 变量名 | 值 |
|--------|-----|
| `VITE_US_API_URL` | `https://api.your-domain.com` |

---

## 参考资源

- [Cloudflare Tunnel 文档](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/)
- [cloudflared GitHub](https://github.com/cloudflare/cloudflared)
- [ngrok 文档](https://ngrok.com/docs)
