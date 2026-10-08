# 前端 VPS 托管化方案（Nginx 静态托管替代 dev server）

> 状态：已实施（2026-10-08 验收通过） | 背景：:5173 Vite dev server 无守护运行，2026-09 中旬死亡后 15 天无人发现

## 实施记录（2026-10-08）

- `frontend/dist` 重新构建（tsc + vite build 通过，3152 modules）；
- nginx 1.24.0 安装；`/etc/nginx/sites-available/stock-frontend` 启用（default 站点已禁用）；
- systemd `Restart=always` drop-in 生效，nginx enabled；
- 验收：公网 5173 200（title "Stock Dashboard"）、SPA fallback 200、资产 200、
  `/api/cn/api/v1/health` 经代理 200；dev server 已停止；
- 坑：/home/ubuntu 为 750 导致 www-data stat() 500，已 `chmod o+x /home/ubuntu`；
- **坑（2026-10-08 修复）：生产构建必须带 API 前缀环境变量**——client.ts 在 DEV 模式用
  `/api/cn`、`/api/us`（vite proxy），生产模式读 `VITE_CN_API_URL`/`VITE_US_API_URL`，
  缺省为空串导致浏览器直调裸 `/api/v1/*`，落入 SPA fallback 拿到 HTML（200），
  前端报"无法连接 API 服务器"。正确构建：
  `VITE_CN_API_URL=/api/cn VITE_US_API_URL=/api/us npm run build`；
- nginx 加防御性 `location /api/` → JSON 404，未知 API 路径不再被 SPA fallback 吞成 HTML；
- dist 更新流程：`cd frontend && VITE_CN_API_URL=/api/cn VITE_US_API_URL=/api/us npm run build`（无需 reload）。

## 1. 背景与目标

当前 `http://134.175.237.24:5173/` 是手工 `npm run dev` 的 Vite dev server：
无进程守护（死了没人知道）、dev server 直接暴露公网（日志可见公网扫描者的路径穿越探测，
虽被 fs.allow 挡住，但攻击面真实存在）、HMR 文件监听浪费资源。

目标：**Nginx 托管静态构建产物 + systemd 守护**，替代 dev server 对外服务。
安全组、端口（5173）、访问 URL 均不变。

## 2. 方案

```
浏览器 ──:5173──> nginx (systemd, 静态只读)
                    ├─ /            → frontend/dist (SPA fallback)
                    ├─ /api/cn/...  → 127.0.0.1:8000（去前缀，等价现 vite proxy）
                    └─ /api/us/...  → 43.167.190.219:8000（海外后端，现状保留）
```

1. **构建**：`cd frontend && npm run build`（tsc + vite build，产出最新 dist；
   当前 dist 是 2026-08-21 旧构建，9 月以来的页面改动需重新构建才生效）；
2. **安装 nginx**：`sudo apt install nginx`（唯一系统级改动；sudo 免密已确认可用）；
3. **站点配置** `/etc/nginx/sites-available/stock-frontend`：
   - `listen 5173 default_server;` `root .../frontend/dist;`
   - SPA fallback：`location / { try_files $uri $uri/ /index.html; }`
   - 资产缓存：`/assets/` 长缓存（带 hash 文件名）；
   - `location /api/cn/ { proxy_pass http://127.0.0.1:8000/; }`（尾斜杠去前缀，与 vite rewrite 等价）；
   - `location /api/us/ { proxy_pass http://43.167.190.219:8000/; proxy_set_header Host $host; }`；
   - 禁用默认站点（监听 80 的 default），`nginx -t` 后 reload；
4. **守护**：nginx 由系统 systemd 单元管理，加 drop-in `Restart=always`，崩溃自动拉起；
5. **停掉 dev server**：关闭 tmux 会话 `vite`（回滚时按 §5 重启）；
6. **文档**：`docs/deployment/PHASE4_DEPLOYMENT.md` 补一节"VPS 直连访问路径"；
   CLAUDE.md 中该 URL 的描述同步修正。

## 3. 明确不做

- 不改动 Cloudflare Pages 正式前端（那是另一条发布通道，与本机直连路径无关）；
- 不上 HTTPS/域名（维持 IP:5173 现状；需要的话另立方案）；
- 不动海外后端代理目标（43.167.190.219:8000 通断是海外服务器的事）；
- 不做 CI/自动构建（dist 更新仍人工 `npm run build`，静态文件即改即生效，无需 reload nginx）。

## 4. 验收

1. `ss -tlnp`：5173 由 nginx 监听，无 node 进程；
2. 公网 `http://134.175.237.24:5173/` 200，页面资产加载正常（版本为最新构建）；
3. `/api/cn/api/v1/health` 经 nginx 代理 200；首页关键数据（看板）能出数；
4. `systemctl is-enabled nginx` / kill -TERM 测试自动拉起（可选，验收时视情况）；
5. dev server 的 tmux 会话已关闭。

## 5. 回滚

```bash
sudo rm /etc/nginx/sites-enabled/stock-frontend && sudo nginx -s reload
tmux new-session -d -s vite -c /home/ubuntu/projects/stock_data/frontend 'npm run dev -- --host 0.0.0.0'
```

## 6. 风险

- nginx 未运行过在这台机器 → 配置错误风险由 `nginx -t` + 灰度（先 keep vite 跑着、nginx 起在
  5173 会冲突）化解：**实施顺序为先停 vite 再启 nginx，若验证失败立即按 §5 回滚**，停机窗口分钟级；
- dist 构建失败（tsc 报错）→ 构建失败则不动线上（先 build 后切流量）。
