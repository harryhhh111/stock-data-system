# Hyperliquid 只读看板运行说明

> 2026-10-18。stock_data 侧（后端 + 前端）已实施并通过测试；依赖 hl-trader 的 `status --json`（另一个会话实施，见 HYPERLIQUID_DASHBOARD_MVP_PLAN.md §4.1 契约）。Rust 侧未就绪前接口返回 503，页面显示加载失败，属预期。

## 启用步骤（US 实例）

1. 后端配置以下环境变量并重启 web 进程（tmux 会话 `stock-web-mvp`：C-c 后 `venv/bin/python -m web`）：

   | 变量 | 说明 |
   |---|---|
   | `STOCK_HL_TRADER_BIN` | hl-trader 可执行文件绝对路径 |
   | `STOCK_HL_TRADER_CONFIG` | 看板最小配置绝对路径（**不含 `secret_file`**，见权限边界） |
   | `STOCK_HL_EXPECTED_ACCOUNT` | 部署预期账户地址（0x，大小写不敏感），与快照不符拒绝展示 |

2. 前端已包含 `/hyperliquid` 路由与导航；发布沿用现有部署：`VITE_CN_API_URL=/api/cn VITE_US_API_URL=/api/us npm run build`，dist 同步到国内 nginx。`/api/us/` 代理已覆盖新路径，不另设跨域。

3. 验证：`curl http://127.0.0.1:8000/api/v1/hyperliquid/dashboard` 返回 `{ok, data}`；schema/network/账户不符时返回 503。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/hyperliquid/dashboard` | 公开只读；后端 30 秒缓存并合并并发请求；响应为契约校验后的快照 |

## 权限边界（部署门槛）

- hl-trader 看板模式不装载私钥、`--json` 不触发写操作；看板配置不含 `secret_file`（需要 hl-trader 支持最小配置，见 §4.1 进程约定）。
- web 服务 OS 用户不得读取交易私钥文件：私钥保持属主隔离 + `0o600`，或用 ACL 显式拒绝 web 用户。当前过渡期如 web 仍与交易同用户运行，须意识到这一剩余风险。

## 数据口径

- 测试网只读快照，来源与查询时间分区标注（每块 `queried_at`）；资金费实际收付窗口为 `window_start`/`window_end`，`window_complete=false` 时合计不完整。
- 缺值显示 `—`，不补零；页面不自动轮询，手动刷新保留旧读数并标注刷新失败。
- 公开访问（2026-10-17 确认）：任何可访问网站者可见测试网账户地址、持仓与资金费盈亏。

## 验证命令

`venv/bin/python -m pytest tests/test_web/test_hyperliquid.py -q`（mock 快照，不连接交易所、不读私钥）
`cd frontend && VITE_CN_API_URL=/api/cn VITE_US_API_URL=/api/us npm run build`
